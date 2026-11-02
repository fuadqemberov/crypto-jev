"""Mixed portfolio, virtual money: the three ideas that held up best in our research, in one account.

    40 %  Funding carry   long spot + short perpetual on the same coin (delta neutral): price moves cancel,
                          the short collects the funding the longs pay. Up to 5 coins among the 20 most liquid,
                          chosen by their average funding over the last 3 days.
    40 %  Trend           the long-only 6h Donchian portfolio from backtest/trend.py at half size (app/trend_paper.py).
    20 %  DCA             BTC and ETH 50/50, bought in 12 equal weekly instalments, then held.

Each sleeve keeps its own cash; the total is their sum. State: data/mix.json (+ data/mix-trend.json for the
trend sleeve). Delete both to start over.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from .config import Settings
from .live import LiveExecutor
from .trend_paper import TrendPaper

log = logging.getLogger('app.mix')
HOUR = 3_600_000
WEIGHTS = {'carry': 0.40, 'trend': 0.40, 'dca': 0.20}

# carry sleeve
CARRY_SLOTS = 5
CARRY_UNIVERSE = 20
CARRY_USE = 0.75                 # share of the sleeve put to work (the rest is margin buffer for the shorts)
CARRY_ENTER = 0.00005            # enter when the 3-day average funding per 8h is above 0.005 % (≈ 5.5 % a year)
CARRY_EXIT = 0.0                 # leave when it turns negative
CARRY_FEE = 0.0025               # one side, both legs: spot 0.1 % + perp 0.05 % + slippage 0.05 % × 2
CARRY_REVIEW = 8 * HOUR
CARRY_MIN_HOLD = 3 * 24 * HOUR

# DCA sleeve
DCA_COINS = ('BTCUSDT', 'ETHUSDT')
DCA_TRANCHES = 12
DCA_EVERY = 7 * 24 * HOUR
DCA_FEE = 0.001


class MixPortfolio:
    def __init__(self, settings: Settings, market: Any) -> None:
        self.settings = settings
        self.market = market
        self.capital = settings.capital
        self.path = settings.data_dir / 'mix.json'
        self.trend = TrendPaper(replace(settings, capital=round(self.capital * WEIGHTS['trend'], 2)), market,
                                settings.data_dir / 'mix-trend.json')
        self.error: dict | None = None
        self.prices: dict[str, float] = {}
        self.state = self._read() or self._fresh()
        self.live = LiveExecutor(settings.data_dir)
        if self.live.enabled:
            log.warning('REAL TİCARƏT AKTİVDİR: Binance hesabında orderlər açılacaq (limit %.0f $)', self.live.capital)

    # -- persistence -----------------------------------------------------------------
    def _fresh(self) -> dict:
        c = self.capital
        return dict(
            started_at=None, btc_start=None, history=[], log=[],
            carry=dict(cash=c * WEIGHTS['carry'], capital=c * WEIGHTS['carry'], positions={}, funding=0.0, fees=0.0,
                       reviewed_at=0),
            dca=dict(cash=c * WEIGHTS['dca'], capital=c * WEIGHTS['dca'], qty={s: 0.0 for s in DCA_COINS},
                     bought=0, last_buy=0, fees=0.0),
        )

    def _read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.warning('%s oxunmadı (%s) — yeni hesab', self.path, exc)
            return None

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state))
        tmp.replace(self.path)

    def _log(self, ts: int, sleeve: str, action: str, symbol: str, amount: float, note: str = '') -> None:
        self.state['log'] = (self.state['log'] + [dict(ts=ts, sleeve=sleeve, action=action, symbol=symbol,
                                                       amount=round(amount, 2), note=note)])[-300:]

    # -- loop ------------------------------------------------------------------------
    async def run(self) -> None:
        trend = asyncio.create_task(self.trend.run())
        try:
            while True:
                try:
                    await self.tick()
                    self.error = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.error = {'at': int(time.time() * 1000), 'message': f'{type(exc).__name__}: {exc}'[:300]}
                    log.warning('Qarışıq portfel xətası: %s', self.error['message'])
                await asyncio.sleep(60)
        finally:
            trend.cancel()

    async def tick(self) -> None:
        quotes = await self.market.quotes()
        now = self.market.now_ms()
        self.prices = {s: q.price for s, q in quotes.items()}
        st = self.state
        if st['started_at'] is None:
            st['started_at'] = now
            st['btc_start'] = self.prices.get('BTCUSDT')
        self._settle_carry(quotes, now)
        if now - st['carry']['reviewed_at'] >= CARRY_REVIEW:
            await self._review_carry(now)
        self._dca(now)
        if not st['history'] or now - st['history'][-1][0] >= HOUR:
            st['history'] = (st['history'] + [[now, round(self.total(), 2), self.prices.get('BTCUSDT')]])[-5000:]
        self._save()
        if self.live.enabled:
            spot, short = self.live_targets()
            await self.live.reconcile(spot, short, self.total())

    def live_targets(self) -> tuple[dict[str, float], dict[str, float]]:
        """What the real account should hold: spot quantities (DCA + trend + carry spot legs) and carry shorts."""
        spot: dict[str, float] = {}
        short: dict[str, float] = {}
        for s, q in self.state['dca']['qty'].items():
            spot[s] = spot.get(s, 0.0) + q
        t = self.trend.state
        for s, w in t['held'].items():
            px = t['prices'].get(s) or self.prices.get(s)
            if px and w > 0:
                spot[s] = spot.get(s, 0.0) + w * t['equity'] / px
        for s, p in self.state['carry']['positions'].items():
            spot[s] = spot.get(s, 0.0) + p['qty']
            short[s] = p['qty']
        return spot, short

    # -- carry -----------------------------------------------------------------------
    def _settle_carry(self, quotes: dict, now: int) -> None:
        """At each funding time the short perp receives qty × mark × rate (pays when the rate is negative)."""
        c = self.state['carry']
        for s, pos in c['positions'].items():
            q = quotes.get(s)
            if not q:
                continue
            if pos.get('next_funding') and now >= pos['next_funding']:
                amount = pos['qty'] * q.price * pos.get('rate', 0.0)
                c['cash'] += amount
                c['funding'] += amount
                pos['earned'] = pos.get('earned', 0.0) + amount
                pos['next_funding'] = 0
            pos['rate'] = q.funding_rate
            if q.next_funding > now:
                pos['next_funding'] = q.next_funding

    async def _avg_funding(self, symbol: str) -> float | None:
        rows = await self.market._get('/fundingRate', symbol=symbol, limit=9)
        rates = [float(r['fundingRate']) for r in rows if 'fundingRate' in r]
        return float(np.mean(rates)) if len(rates) >= 6 else None

    async def _review_carry(self, now: int) -> None:
        c = self.state['carry']
        ranked = [s for s, _ in await self.market.universe()][:CARRY_UNIVERSE]
        candidates = list(dict.fromkeys(ranked + list(c['positions'])))
        avg: dict[str, float] = {}
        for i in range(0, len(candidates), 5):
            batch = candidates[i:i + 5]
            res = await asyncio.gather(*(self._avg_funding(s) for s in batch), return_exceptions=True)
            avg.update({s: r for s, r in zip(batch, res) if isinstance(r, float)})
        # exits: funding turned negative (after the minimum hold) or the coin left the liquid list
        for s in list(c['positions']):
            pos = c['positions'][s]
            held = now - pos['opened_at']
            bad = avg.get(s, -1.0) < CARRY_EXIT or s not in ranked
            if bad and held >= CARRY_MIN_HOLD and s in self.prices:
                self._carry_close(s, now, f'orta funding {avg.get(s, 0) * 100:.4f}%')
        # entries: best average funding first, into free slots
        free = CARRY_SLOTS - len(c['positions'])
        best = sorted((s for s in ranked if s not in c['positions'] and avg.get(s, -1) > CARRY_ENTER and s in self.prices),
                      key=lambda s: -avg[s])
        for s in best[:max(free, 0)]:
            notional = self.carry_equity() * CARRY_USE / CARRY_SLOTS
            if notional < 10 or c['cash'] < notional * CARRY_FEE:
                break
            fee = notional * CARRY_FEE
            c['cash'] -= fee
            c['fees'] += fee
            price = self.prices[s]
            c['positions'][s] = dict(qty=notional / price, entry=price, notional=notional, opened_at=now,
                                     avg_funding=avg[s], earned=0.0, rate=0.0, next_funding=0)
            self._log(now, 'carry', 'AÇ', s, notional, f'3 günlük orta funding {avg[s] * 100:.4f}% / 8s')
        c['reviewed_at'] = now

    def _carry_close(self, s: str, now: int, note: str) -> None:
        c = self.state['carry']
        pos = c['positions'].pop(s)
        fee = pos['qty'] * self.prices[s] * CARRY_FEE
        c['cash'] -= fee
        c['fees'] += fee
        self._log(now, 'carry', 'BAĞLA', s, pos['earned'] - fee, note)

    def carry_equity(self) -> float:
        # delta neutral: the spot gain/loss and the perp loss/gain cancel, so the value is cash
        return self.state['carry']['cash']

    # -- DCA -------------------------------------------------------------------------
    def _dca(self, now: int) -> None:
        d = self.state['dca']
        if d['bought'] >= DCA_TRANCHES or (d['last_buy'] and now - d['last_buy'] < DCA_EVERY):
            return
        if any(s not in self.prices for s in DCA_COINS):
            return
        amount = d['cash'] / (DCA_TRANCHES - d['bought'])
        for s in DCA_COINS:
            part = amount / len(DCA_COINS)
            d['qty'][s] += part * (1 - DCA_FEE) / self.prices[s]
            d['fees'] += part * DCA_FEE
            self._log(now, 'dca', 'AL', s, part, f'{d["bought"] + 1}/{DCA_TRANCHES}')
        d['cash'] -= amount
        d['bought'] += 1
        d['last_buy'] = now

    def dca_equity(self) -> float:
        d = self.state['dca']
        return d['cash'] + sum(q * self.prices.get(s, 0.0) for s, q in d['qty'].items())

    # -- totals / API ----------------------------------------------------------------
    def total(self) -> float:
        return self.carry_equity() + self.trend.state['equity'] + self.dca_equity()

    def summary(self) -> dict:
        st = self.state
        c, d, t = st['carry'], st['dca'], self.trend.summary()
        total = self.total()
        hist = [h[1] for h in st['history']] or [self.capital]
        eq = np.array([self.capital] + hist)
        dd = float((1 - eq / np.maximum.accumulate(eq)).max() * 100)
        btc_now = self.prices.get('BTCUSDT')
        btc = (btc_now / st['btc_start'] - 1) * 100 if st['btc_start'] and btc_now else None
        sleeves = [
            dict(key='carry', name='Funding arbitrajı', share=WEIGHTS['carry'], capital=c['capital'], equity=self.carry_equity(),
                 note=f"{len(c['positions'])}/{CARRY_SLOTS} coin · funding {c['funding']:+.2f} $ · komissiya {c['fees']:.2f} $"),
            dict(key='trend', name='Trend portfeli', share=WEIGHTS['trend'], capital=t['capital'], equity=t['equity'],
                 note=f"{len(t['positions'])} coin · bazarda {t['gross'] * 100:.0f}% · 6 saatdan bir"),
            dict(key='dca', name='DCA (BTC + ETH)', share=WEIGHTS['dca'], capital=d['capital'], equity=self.dca_equity(),
                 note=f"{d['bought']}/{DCA_TRANCHES} həftəlik alış"),
        ]
        for s in sleeves:
            s['return_pct'] = (s['equity'] / s['capital'] - 1) * 100 if s['capital'] else 0.0
        carry_pos = [dict(symbol=s, notional=p['qty'] * self.prices.get(s, p['entry']), earned=p.get('earned', 0.0),
                          rate=p.get('rate', 0.0), avg_funding=p['avg_funding'], opened_at=p['opened_at'])
                     for s, p in c['positions'].items()]
        btc_curve = [[h[0], self.capital * h[2] / st['btc_start']] for h in st['history'] if st['btc_start'] and h[2]]
        return dict(capital=self.capital, equity=total, return_pct=(total / self.capital - 1) * 100, max_dd_pct=dd,
                    started_at=st['started_at'], btc_return_pct=btc, sleeves=sleeves, carry_positions=carry_pos,
                    trend_positions=t['positions'], dca_qty=d['qty'], log=list(reversed(st['log'][-60:])) +
                    [dict(ts=x['ts'], sleeve='trend', action='BALANS', symbol=x['symbol'], amount=x['value'],
                          note=f"{x['from_w'] * 100:.1f}% → {x['to_w'] * 100:.1f}%") for x in t['trades'][:20]],
                    curve=[h[:2] for h in st['history']], btc_curve=btc_curve,
                    error=self.error or t['error'], live=self.live.summary())
