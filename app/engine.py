"""Live paper-trading engine: 20 strategies, 20 independent virtual wallets.

Every ``PRICE_REFRESH_SECONDS`` all prices are refreshed and every account is marked to market.
When a new candle closes, the universe (top-N USDT pairs by 24h volume) is refreshed, closed
candles are loaded and each strategy from ``backtest.strategies`` is evaluated on the bar close
exactly as in the backtester:

* exit  – the strategy holds the coin and its position state on the closed bar is 0;
* entry – the state switched 0 → 1 on the closed bar (a fresh signal) and the wallet has a free slot.

Orders are filled at the current price (≈ next bar open) with fee + slippage. No real orders exist.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

import numpy as np
import pandas as pd

from backtest.strategies import STRATEGIES, Strategy

from .config import Settings
from .ledger import Ledger
from .store import Store

log = logging.getLogger('app.engine')
BENCHMARK = '_benchmark'
MAX_POINTS = 400


def downsample(points: list[tuple[int, float]], limit: int = MAX_POINTS) -> list[tuple[int, float]]:
    if len(points) <= limit:
        return points
    step = len(points) / (limit - 1)
    return [points[int(i * step)] for i in range(limit - 1)] + [points[-1]]


def evaluate(strategies: list[Strategy], data: dict[str, pd.DataFrame]) -> dict[str, dict[str, tuple[int, int]]]:
    """(previous, current) position state of every strategy for every symbol on the last closed bar."""
    out: dict[str, dict[str, tuple[int, int]]] = {s.key: {} for s in strategies}
    for symbol, df in data.items():
        for s in strategies:
            try:
                pos = np.nan_to_num(np.asarray(s.fn(df), dtype=float))
            except Exception:  # one broken frame must not stop the other 19 strategies
                log.exception('%s %s strategiya xətası', s.key, symbol)
                continue
            if len(pos) >= 2:
                out[s.key][symbol] = (int(pos[-2] > 0.5), int(pos[-1] > 0.5))
    return out


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
        self.prices: dict[str, float] = {}
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
        self.prices = await self.market.prices()
        self.last_price_at = int(time.time() * 1000)
        equity = self.ledger.update_prices(self.prices)
        now = self.market.now_ms()
        closed = now // self.interval * self.interval - self.interval
        if (self.last_candle is None or closed > self.last_candle) and \
                now - (closed + self.interval) >= self.settings.candle_delay_seconds * 1000:
            await self.process_candle(closed)
            equity = self.ledger.update_prices({})
        if now - self.last_snapshot >= self.interval // 12:
            self.snapshot(now, equity)
        self.last_error = None

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
            raise RuntimeError('Ticarət üçün heç bir USDT cütü tapılmadı')
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
        states = await asyncio.to_thread(evaluate, self.strategies, data)

        ts = self.market.now_ms()
        in_universe = set(ranked)
        new_signals: dict[str, dict] = {}
        for s in self.strategies:
            st = states[s.key]
            acc = self.ledger.accounts[s.key]
            exits, entries, skipped = [], [], []
            for symbol in list(acc.positions):
                if symbol in st and st[symbol][1] == 0:
                    trade = self.ledger.sell(s.key, symbol, self._price(symbol, data), ts, 'signal')
                    if trade:
                        exits.append(symbol)
                        self._event(ts, s, 'SELL', symbol, trade['exit_price'], trade['pnl'], trade['ret'])
            fresh = [sym for sym in ranked if sym in st and st[sym] == (0, 1)]
            for symbol in fresh:
                if symbol in acc.positions:
                    continue
                pos = self.ledger.buy(s.key, symbol, self._price(symbol, data), ts)
                if pos:
                    entries.append(symbol)
                    self._event(ts, s, 'BUY', symbol, pos.entry_price, None, None)
                else:
                    skipped.append(symbol)
            active = [sym for sym in ranked if sym in st and st[sym][1] == 1]
            new_signals[s.key] = {'candle': closed, 'active': active, 'entries': entries, 'exits': exits,
                                  'skipped': skipped, 'evaluated': sum(1 for sym in st if sym in in_universe)}
        self.signals = new_signals
        self.last_candle = closed
        self.candle_seconds = round(time.monotonic() - started, 2)
        self.store.set_meta('signals', self.signals)
        self.store.set_meta('universe', self.universe)
        self.store.set_meta('events', list(self.events))
        self.store.set_meta('last_candle', closed)
        log.info('Şam %s işləndi: %d coin, %.1f s', closed, len(data), self.candle_seconds)

    def _price(self, symbol: str, data: dict[str, pd.DataFrame]) -> float:
        price = self.prices.get(symbol)
        if not price and symbol in data:
            price = float(data[symbol].close.iloc[-1])
        return price or self.ledger.last_price.get(symbol, 0.0)

    def _event(self, ts: int, s: Strategy, side: str, symbol: str, price: float, pnl: float | None, ret: float | None) -> None:
        self.events.appendleft({'ts': ts, 'key': s.key, 'name': s.name, 'side': side, 'symbol': symbol,
                                'price': price, 'pnl': pnl, 'ret': ret})

    # -- read models for the dashboard ----------------------------------------------------
    def _summary(self, key: str, stats: dict) -> dict:
        acc = self.ledger.accounts[key]
        st = stats.get(key) or {}
        equity = self.ledger.equity(key)
        invested = sum(self.ledger.liquidation_value(p) for p in acc.positions.values())
        unrealized = sum(self.ledger.liquidation_value(p) - p.cost for p in acc.positions.values())
        n = int(st.get('n') or 0)
        gross_win, gross_loss = float(st.get('gross_win') or 0), float(st.get('gross_loss') or 0)
        s = self.by_key[key]
        return {
            'key': key, 'order': self.strategies.index(s) + 1, 'name': s.name, 'family': s.family, 'capital': acc.capital, 'equity': equity,
            'return_pct': (equity / acc.capital - 1) * 100, 'cash': acc.cash, 'invested': invested,
            'exposure_pct': invested / equity * 100 if equity > 0 else 0.0,
            'realized': float(st.get('pnl') or 0), 'unrealized': unrealized, 'fees': float(st.get('fees') or 0),
            'trades': n, 'wins': int(st.get('wins') or 0),
            'win_rate': (int(st.get('wins') or 0) / n * 100) if n else None,
            'profit_factor': (gross_win / gross_loss) if gross_loss > 0 else (None if gross_win == 0 else 'inf'),
            'avg_trade_pct': float(st['avg_ret']) * 100 if n else None,
            'best_trade_pct': float(st['best']) * 100 if n else None,
            'worst_trade_pct': float(st['worst']) * 100 if n else None,
            'avg_hold_hours': float(st['avg_hold']) / 3_600_000 if n else None,
            'max_dd_pct': acc.max_dd * 100, 'open_positions': len(acc.positions),
            'signals_active': len(self.signals.get(key, {}).get('active', [])),
            'started_at': acc.started_at,
        }

    def status(self) -> dict:
        now = self.market.now_ms()
        return {
            'mode': 'demo' if self.market.demo else 'live', 'timeframe': self.settings.timeframe,
            'now': now, 'last_candle': self.last_candle,
            'next_candle': (now // self.interval + 1) * self.interval,
            'last_price_at': self.last_price_at, 'universe': len(self.universe),
            'symbols': 'ALL' if self.settings.symbols == ('ALL',) else ','.join(self.settings.symbols),
            'top_n': self.settings.top_n, 'capital': self.settings.capital, 'max_positions': self.settings.max_positions,
            'fee_pct': self.settings.fee_rate * 100, 'slippage_pct': self.settings.slippage_rate * 100,
            'candle_seconds': self.candle_seconds, 'error': self.last_error,
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
            price = self.ledger.last_price.get(p.symbol, p.entry_price)
            value = self.ledger.liquidation_value(p)
            positions.append({'symbol': p.symbol, 'qty': p.qty, 'entry_price': p.entry_price, 'price': price,
                              'cost': p.cost, 'value': value, 'pnl': value - p.cost, 'ret_pct': (value / p.cost - 1) * 100,
                              'opened_at': p.opened_at})
        positions.sort(key=lambda p: p['opened_at'], reverse=True)
        rank = {r['key']: i for i, r in enumerate(sorted(
            (self._summary(k, stats) for k in self.by_key), key=lambda r: r['equity'], reverse=True), 1)}
        return {
            'status': self.status(), 'summary': {**self._summary(key, stats), 'rank': rank[key]},
            'rules': s.rules, 'positions': positions, 'trades': self.store.trades(key, 300),
            'signals': self.signals.get(key, {'active': [], 'entries': [], 'exits': [], 'skipped': []}),
            'equity': downsample(self.store.equity(key)), 'benchmark': downsample(self.store.equity(BENCHMARK)),
            'benchmark_symbol': self.benchmark['symbol'] if self.benchmark else None,
        }
