"""Live paper-trading engine: 30 scalping strategies, 30 independent virtual USDT-M futures wallets.

Every ``PRICE_REFRESH_SECONDS`` mark prices and funding are refreshed and, for every account:
funding is settled when a funding time passes, positions hitting maintenance margin are
liquidated, positions crossing their stop-loss are closed and positions whose net PnL reached
``TAKE_PROFIT_USDT`` are closed.

When a new candle closes the universe (top-N USDT perpetuals by 24h volume) is refreshed, closed
candles are loaded and each strategy decides a direction (long / short / flat, see app/futures.py):

* close  – the held direction is no longer the strategy's direction on the closed bar (only for rules
           that exit on their own signal, see SIGNAL_EXITS; the others end at stop / TP / MAX_HOLD_HOURS);
* open   – the direction changed on the closed bar (fresh signal); leverage is chosen by the
           strategy, margin = ORDER_MARGIN_PCT of equity. Skipped when fees would exceed MAX_COST_R of
           the stop distance, or within COOLDOWN_BARS candles of this strategy's last close on the coin. No limit on parallel positions —
           only the free margin of the wallet. One position per coin per strategy.

Orders are filled at the current mark price with fee + slippage. No real orders exist.

Pause: ``set_entries_paused(True)`` (dashboard button "Yeni siqnalları dayandır") stops new entries for
every strategy. Open positions keep running to their stop / take-profit / signal exit. Persisted.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

import pandas as pd

from backtest.scalp import STRATEGIES
from backtest.strategies import Strategy

from .config import Settings
from .futures import Signal, cost_r, evaluate, exits_on_signal, leverage, leverage_rule, stop_distance
from .ledger import Ledger, Position
from .store import Store

log = logging.getLogger('app.engine')
BENCHMARK = '_benchmark'
MAX_POINTS = 400
SIDE = {1: 'LONG', -1: 'SHORT'}


def downsample(points: list[tuple[int, float]], limit: int = MAX_POINTS) -> list[tuple[int, float]]:
    if len(points) <= limit:
        return points
    step = len(points) / (limit - 1)
    return [points[int(i * step)] for i in range(limit - 1)] + [points[-1]]


class Engine:
    def __init__(self, settings: Settings, market: Any, store: Store, strategies: list[Strategy] | None = None) -> None:
        self.settings = settings
        self.market = market
        self.store = store
        self.strategies = list(strategies or STRATEGIES)
        self.by_key = {s.key: s for s in self.strategies}
        self.interval = settings.interval_ms
        self.ledger = Ledger(settings, store, list(self.by_key), market.now_ms())
        self.last_candle: int | None = store.get_meta('last_candle')
        self.signals: dict[str, dict] = store.get_meta('signals', {})
        self.universe: list[tuple[str, float]] = store.get_meta('universe', [])
        self.benchmark: dict | None = store.get_meta('benchmark')
        self.events: deque[dict] = deque(store.get_meta('events', []), maxlen=300)
        pause = store.get_meta('entries_paused') or {}
        self.entries_paused: bool = bool(pause.get('paused'))
        self.paused_at: int | None = pause.get('at')
        self.last_close: dict[tuple[str, str], int] = store.last_closes()   # cooldown bookkeeping
        # warm-up: no new orders right after a (re)start — the first candle processed may be old
        self.warmup_until = market.now_ms() + settings.startup_delay_seconds * 1000
        self.prices: dict[str, float] = {}
        self.funding: dict[str, tuple[int, float]] = {}      # symbol -> (next settlement ms, rate)
        self.last_price_at: int | None = None
        self.last_snapshot = 0
        self.last_error: dict | None = None
        self.candle_seconds: float | None = None
        if self.last_candle is not None and hasattr(market, 'align'):
            market.align(self.last_candle + self.interval)

    # -- loop -------------------------------------------------------------------------
    async def run(self) -> None:
        pause = self.settings.price_seconds
        if self.market.demo:
            pause = min(pause, max(1.0, self.settings.demo_candle_seconds / 5))
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = {'at': int(time.time() * 1000), 'message': f'{type(exc).__name__}: {exc}'[:300]}
                log.warning('Engine dövrü xətası: %s', self.last_error['message'])
            await asyncio.sleep(pause)

    async def tick(self) -> None:
        quotes = await self.market.quotes()
        now = self.market.now_ms()
        self.prices = {s: q.price for s, q in quotes.items()}
        self.last_price_at = int(time.time() * 1000)
        self.ledger.update_prices(self.prices)
        self._settle_funding(quotes, now)
        self._risk_checks(now)
        closed = now // self.interval * self.interval - self.interval
        if (self.last_candle is None or closed > self.last_candle) and \
                now - (closed + self.interval) >= self.settings.candle_delay_seconds * 1000:
            await self.process_candle(closed)
        equity = self.ledger.update_prices({})
        if now - self.last_snapshot >= max(self.interval // 12, 60_000):   # at most one equity row per minute
            self.snapshot(now, equity)
        self.last_error = None

    def _settle_funding(self, quotes: dict, now: int) -> None:
        held = self.ledger.held_symbols()
        for symbol, (when, rate) in list(self.funding.items()):
            if when and now >= when and symbol in held and symbol in self.prices:
                self.ledger.apply_funding(symbol, rate, self.prices[symbol], when)
        for symbol, q in quotes.items():
            self.funding[symbol] = (q.next_funding, q.funding_rate)

    def _risk_checks(self, now: int) -> None:
        tp = self.settings.take_profit
        hold_ms = int(self.settings.max_hold_hours * 3_600_000)
        for s in self.strategies:
            acc = self.ledger.accounts[s.key]
            for symbol, pos in list(acc.positions.items()):
                price = self.prices.get(symbol)
                if not price:
                    continue
                if self.ledger.is_liquidated(pos, price):
                    self._close(s, pos, self.ledger.liquidation_price(pos) or price, now, 'liquidation')
                elif self.ledger.is_stopped(pos, price):
                    self._close(s, pos, price, now, 'stop_loss')          # stop-market: filled at the current mark
                elif self.ledger.is_target(pos, price):
                    self._close(s, pos, price, now, 'take_profit')        # R-multiple target reached
                elif hold_ms and now - pos.opened_at >= hold_ms:
                    self._close(s, pos, price, now, 'timeout')            # held too long without stop / TP
                elif tp > 0 and self.ledger.net_pnl(pos, price) >= tp:
                    self._close(s, pos, price, now, 'take_profit')

    def benchmark_equity(self) -> float | None:
        b = self.benchmark
        price = self.prices.get(b['symbol']) if b else None
        if not b or not price:
            return None
        f, s = self.settings.fee_rate, self.settings.slippage_rate
        qty = self.settings.capital * (1 - f) / (b['price'] * (1 + s))
        return qty * price * (1 - s) * (1 - f)

    def snapshot(self, now: int, equity: dict[str, float]) -> None:
        values = dict(equity)
        bench = self.benchmark_equity()
        if bench is not None:
            values[BENCHMARK] = bench
        self.store.add_equity(now, values)
        self.last_snapshot = now

    # -- candle processing --------------------------------------------------------------
    async def _load(self, symbols: list[str], closed: int) -> dict[str, pd.DataFrame]:
        sem = asyncio.Semaphore(self.settings.kline_parallelism)

        async def one(symbol: str) -> tuple[str, pd.DataFrame | None]:
            async with sem:
                try:
                    return symbol, await self.market.klines(symbol, self.settings.history_bars)
                except Exception as exc:
                    log.warning('%s şamları alınmadı: %s', symbol, type(exc).__name__)
                    return symbol, None

        out = {}
        for symbol, df in await asyncio.gather(*(one(s) for s in symbols)):
            # Only use frames whose last bar IS the candle that just closed (no stale data).
            if df is not None and len(df) >= 60 and int(df.index[-1]) == closed:
                out[symbol] = df
        return out

    async def process_candle(self, closed: int) -> None:
        started = time.monotonic()
        try:
            self.universe = [(s, float(v)) for s, v in await self.market.universe()]
        except Exception as exc:
            if not self.universe:
                raise
            log.warning('Universe yenilənmədi, köhnə siyahı istifadə edilir: %s', type(exc).__name__)
        if not self.universe:
            raise RuntimeError('Ticarət üçün heç bir USDT perpetual tapılmadı')
        ranked = [s for s, _ in self.universe]
        if self.benchmark is None:
            symbol = 'BTCUSDT' if 'BTCUSDT' in self.prices else ranked[0]
            if self.prices.get(symbol):
                self.benchmark = {'symbol': symbol, 'price': self.prices[symbol], 'ts': self.market.now_ms()}
                self.store.set_meta('benchmark', self.benchmark)
        wanted = list(dict.fromkeys(ranked + sorted(self.ledger.held_symbols())))
        data = await self._load(wanted, closed)
        if not data:
            raise RuntimeError('Bağlanmış şam məlumatı alınmadı')
        ts = self.market.now_ms()
        self._candle_liquidations(data, closed, ts)
        states = await asyncio.to_thread(evaluate, self.strategies, data)

        in_universe = set(ranked)
        new_signals: dict[str, dict] = {}
        for s in self.strategies:
            st: dict[str, Signal] = states[s.key]
            acc = self.ledger.accounts[s.key]
            exits, entries, skipped, paused, filtered = [], [], [], [], []
            signal_exit = exits_on_signal(s.key, self.settings.signal_exits)
            for symbol, pos in list(acc.positions.items()):
                sig = st.get(symbol)
                if sig and sig.cur != pos.side and signal_exit:
                    trade = self._close(s, pos, self._price(symbol, data), ts, 'reverse' if sig.cur == -pos.side else 'signal')
                    if trade:
                        exits.append({'symbol': symbol, 'side': SIDE[pos.side], 'pnl': trade['pnl']})
            for symbol in ranked:
                sig = st.get(symbol)
                if not sig or sig.cur == 0 or sig.prev == sig.cur or symbol in acc.positions:
                    continue
                if ts < self.warmup_until:                # just (re)started: possibly stale candle, wait
                    filtered.append({'symbol': symbol, 'side': SIDE[sig.cur], 'why': 'startup'})
                    continue
                if self.entries_paused:                   # new entries stopped; open positions keep running
                    paused.append({'symbol': symbol, 'side': SIDE[sig.cur]})
                    continue
                last = self.last_close.get((s.key, symbol))
                if self.settings.cooldown_bars and last is not None and ts - last < self.settings.cooldown_bars * self.interval:
                    filtered.append({'symbol': symbol, 'side': SIDE[sig.cur], 'why': 'cooldown'})
                    continue
                cr = cost_r(s.key, sig.atr_pct, self.settings.fee_rate + self.settings.slippage_rate)
                if self.settings.max_cost_r > 0 and cr > self.settings.max_cost_r:
                    filtered.append({'symbol': symbol, 'side': SIDE[sig.cur], 'why': 'fee', 'cost_r': round(cr, 3)})
                    continue
                lev = leverage(s.key, sig.atr_pct, self.settings.max_leverage, self.settings.risk_per_move)
                px = self._price(symbol, data)
                pos = self.ledger.open(s.key, symbol, sig.cur, lev, px, ts, stop_distance(s.key, sig.atr_pct, px))
                if pos:
                    entries.append({'symbol': symbol, 'side': SIDE[sig.cur], 'leverage': lev})
                    self._event(ts, s, 'OPEN', pos, pos.entry_price)
                else:
                    skipped.append({'symbol': symbol, 'side': SIDE[sig.cur], 'leverage': lev})
            new_signals[s.key] = {
                'candle': closed, 'entries': entries, 'exits': exits, 'skipped': skipped, 'paused': paused, 'filtered': filtered,
                'long': [sym for sym in ranked if sym in st and st[sym].cur == 1],
                'short': [sym for sym in ranked if sym in st and st[sym].cur == -1],
                'evaluated': sum(1 for sym in st if sym in in_universe)}
        self.signals = new_signals
        self.last_candle = closed
        self.candle_seconds = round(time.monotonic() - started, 2)
        self.store.set_meta('signals', self.signals)
        self.store.set_meta('universe', self.universe)
        self.store.set_meta('events', list(self.events))
        self.store.set_meta('last_candle', closed)
        log.info('Şam %s işləndi: %d coin, %.1f s', closed, len(data), self.candle_seconds)

    def set_entries_paused(self, paused: bool) -> dict:
        if bool(paused) != self.entries_paused:
            self.entries_paused = bool(paused)
            self.paused_at = self.market.now_ms() if paused else None
            self.store.set_meta('entries_paused', {'paused': self.entries_paused, 'at': self.paused_at})
            log.info('Yeni girişlər %s', 'DAYANDIRILDI' if paused else 'yenidən AÇILDI')
        return {'entries_paused': self.entries_paused, 'paused_at': self.paused_at,
                'open_positions': sum(len(a.positions) for a in self.ledger.accounts.values())}

    def _candle_liquidations(self, data: dict[str, pd.DataFrame], closed: int, ts: int) -> None:
        """Wicks between two price polls: for a position held through the whole candle, a low (long) /
        high (short) through the stop closes it at the stop; one through the liquidation price
        without a stop in between liquidates it. The stop always sits before the liquidation price."""
        for s in self.strategies:
            for symbol, pos in list(self.ledger.accounts[s.key].positions.items()):
                if symbol not in data or pos.opened_at > closed:
                    continue
                bar = data[symbol].iloc[-1]
                extreme = bar.low if pos.side > 0 else bar.high
                liq = self.ledger.liquidation_price(pos)
                if self.ledger.is_stopped(pos, extreme):
                    self._close(s, pos, pos.stop_price, ts, 'stop_loss')
                elif (pos.side > 0 and bar.low <= liq) or (pos.side < 0 and bar.high >= liq):
                    self._close(s, pos, liq, ts, 'liquidation')
                elif self.ledger.is_target(pos, bar.high if pos.side > 0 else bar.low):
                    self._close(s, pos, pos.tp_price, ts, 'take_profit')     # wick reached the target

    def _close(self, s: Strategy, pos: Position, price: float, ts: int, reason: str) -> dict | None:
        trade = self.ledger.close(s.key, pos.symbol, price, ts, reason)
        if trade:
            self.last_close[(s.key, pos.symbol)] = ts
            self._event(ts, s, 'CLOSE', pos, price, trade)
        return trade

    def _price(self, symbol: str, data: dict[str, pd.DataFrame]) -> float:
        price = self.prices.get(symbol)
        if not price and symbol in data:
            price = float(data[symbol].close.iloc[-1])
        return price or self.ledger.last_price.get(symbol, 0.0)

    def _event(self, ts: int, s: Strategy, action: str, pos: Position, price: float, trade: dict | None = None) -> None:
        self.events.appendleft({'ts': ts, 'key': s.key, 'name': s.name, 'action': action, 'side': SIDE[pos.side],
                                'leverage': pos.leverage, 'symbol': pos.symbol, 'price': price,
                                'pnl': trade['pnl'] if trade else None, 'ret': trade['ret'] if trade else None,
                                'reason': trade['reason'] if trade else None})

    # -- read models for the dashboard ----------------------------------------------------
    def _summary(self, key: str, stats: dict) -> dict:
        acc = self.ledger.accounts[key]
        st = stats.get(key) or {}
        equity = self.ledger.equity(key)
        positions = list(acc.positions.values())
        unrealized = sum(self.ledger.net_pnl(p) for p in positions)
        notional = sum(p.qty * self.ledger.mark(p) for p in positions)
        n = int(st.get('n') or 0)
        gross_win, gross_loss = float(st.get('gross_win') or 0), float(st.get('gross_loss') or 0)
        s = self.by_key[key]
        return {
            'key': key, 'order': self.strategies.index(s) + 1, 'name': s.name, 'family': s.family,
            'capital': acc.capital, 'equity': equity, 'return_pct': (equity / acc.capital - 1) * 100,
            'cash': acc.cash, 'used_margin': sum(p.margin for p in positions), 'notional': notional,
            'effective_leverage': notional / equity if equity > 0 else 0.0,
            'realized': float(st.get('pnl') or 0), 'unrealized': unrealized, 'fees': float(st.get('fees') or 0),
            'funding': float(st.get('funding') or 0) + sum(p.funding for p in positions),
            'trades': n, 'wins': int(st.get('wins') or 0),
            'win_rate': (int(st.get('wins') or 0) / n * 100) if n else None,
            'profit_factor': (gross_win / gross_loss) if gross_loss > 0 else (None if gross_win == 0 else 'inf'),
            'avg_trade_pct': float(st['avg_ret']) * 100 if n else None,
            'best_trade_pct': float(st['best']) * 100 if n else None,
            'worst_trade_pct': float(st['worst']) * 100 if n else None,
            'avg_hold_hours': float(st['avg_hold']) / 3_600_000 if n else None,
            'avg_leverage': float(st['avg_lev']) if n else None,
            'take_profits': int(st.get('tp') or 0), 'liquidations': int(st.get('liq') or 0),
            'stop_losses': int(st.get('sl') or 0), 'timeouts': int(st.get('tmo') or 0),
            'signal_exit': exits_on_signal(key, self.settings.signal_exits),
            'long_trades': int(st.get('longs') or 0), 'short_trades': int(st.get('shorts') or 0),
            'max_dd_pct': acc.max_dd * 100, 'open_positions': len(positions),
            'open_long': sum(1 for p in positions if p.side > 0), 'open_short': sum(1 for p in positions if p.side < 0),
            'started_at': acc.started_at,
        }

    def status(self) -> dict:
        now = self.market.now_ms()
        return {
            'mode': 'demo' if self.market.demo else 'live', 'market': 'USDT-M futures', 'timeframe': self.settings.timeframe,
            'now': now, 'last_candle': self.last_candle,
            'next_candle': (now // self.interval + 1) * self.interval,
            'last_price_at': self.last_price_at, 'universe': len(self.universe),
            'symbols': 'ALL' if self.settings.symbols == ('ALL',) else ','.join(self.settings.symbols),
            'top_n': self.settings.top_n, 'capital': self.settings.capital,
            'margin_pct': self.settings.margin_pct, 'take_profit': self.settings.take_profit,
            'stop_loss_pct': self.settings.stop_loss_pct, 'tp_r_multiple': self.settings.tp_r_multiple,
            'risk_per_move': self.settings.risk_per_move, 'max_cost_r': self.settings.max_cost_r,
            'signal_exits': self.settings.signal_exits, 'cooldown_bars': self.settings.cooldown_bars,
            'max_hold_hours': self.settings.max_hold_hours,
            'max_leverage': self.settings.max_leverage,
            'fee_pct': self.settings.fee_rate * 100, 'slippage_pct': self.settings.slippage_rate * 100,
            'candle_seconds': self.candle_seconds, 'error': self.last_error,
            'entries_paused': self.entries_paused, 'paused_at': self.paused_at,
            'warmup_until': self.warmup_until if now < self.warmup_until else None,
            'open_positions': sum(len(a.positions) for a in self.ledger.accounts.values()),
        }

    def overview(self) -> dict:
        stats = self.store.trade_stats()
        rows = [self._summary(k, stats) for k in self.by_key]
        rows.sort(key=lambda r: r['equity'], reverse=True)
        for rank, r in enumerate(rows, 1):
            r['rank'] = rank
        bench = self.benchmark_equity()
        return {
            'status': self.status(), 'strategies': rows, 'events': list(self.events)[:40],
            'benchmark': {'symbol': self.benchmark['symbol'], 'equity': bench,
                          'return_pct': (bench / self.settings.capital - 1) * 100} if self.benchmark and bench else None,
        }

    def curves(self) -> dict:
        keys = list(self.by_key) + [BENCHMARK]
        return {k: downsample(self.store.equity(k), 250) for k in keys}

    def detail(self, key: str) -> dict:
        s = self.by_key[key]
        acc = self.ledger.accounts[key]
        stats = self.store.trade_stats()
        positions = []
        for p in acc.positions.values():
            pnl = self.ledger.net_pnl(p)
            positions.append({'symbol': p.symbol, 'side': SIDE[p.side], 'leverage': p.leverage, 'qty': p.qty,
                              'entry_price': p.entry_price, 'price': self.ledger.mark(p), 'margin': p.margin,
                              'notional': p.qty * self.ledger.mark(p), 'liq_price': self.ledger.liquidation_price(p),
                              'stop_price': p.stop_price or None, 'tp_price': p.tp_price or None,
                              'pnl': pnl, 'ret_pct': pnl / p.initial_margin * 100, 'funding': p.funding,
                              'opened_at': p.opened_at})
        positions.sort(key=lambda p: p['opened_at'], reverse=True)
        ranked = sorted((self._summary(k, stats) for k in self.by_key), key=lambda r: r['equity'], reverse=True)
        rank = {r['key']: i for i, r in enumerate(ranked, 1)}
        trades = self.store.trades(key, 300)
        for t in trades:
            t['side'] = SIDE[t['side']]
        return {
            'status': self.status(), 'summary': {**self._summary(key, stats), 'rank': rank[key]},
            'rules': s.rules, 'short_rule': 'SHORT: eyni qayda qiymətin güzgü seriyasına (1/qiymət) tətbiq olunur — '
                                            'yəni bazarın tam əksi üçün alış siqnalı SHORT açır.',
            'leverage_rule': leverage_rule(key, self.settings.max_leverage, self.settings.risk_per_move),
            'positions': positions, 'trades': trades,
            'signals': self.signals.get(key, {'entries': [], 'exits': [], 'skipped': [], 'paused': [], 'filtered': [], 'long': [], 'short': []}),
            'equity': downsample(self.store.equity(key)), 'benchmark': downsample(self.store.equity(BENCHMARK)),
            'benchmark_symbol': self.benchmark['symbol'] if self.benchmark else None,
        }
