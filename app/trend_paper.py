"""Live forward test of the trend portfolio — virtual money, frozen parameters.

The one variant that came closest in the 2-year backtest (backtest/trend.py ``long_only_6h``), at half
size (target volatility 12.5 % instead of 25 %, so the backtest's ~38 % drawdown becomes ~19 %).
Nothing here is tuned any more: the point is to see whether it behaves on data that did not exist
when it was chosen.

Every 6 hours (UTC 00/06/12/18, a minute after the candle closes) it downloads the last ~105 days
of 6h futures candles for the most liquid perpetuals, computes the same target weights as the
backtest, marks the book to market, then re-trades coins whose target moved more than 2 % of
equity — with the same fee + slippage (0.1 % per side) and funding (0.01 % / 8 h on longs).
State lives in ``data/trend-paper.json``; delete it to start over.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtest.trend import TrendParams, target_weights, warmup_bars

from .config import Settings
from .market import frame

log = logging.getLogger('app.trend')
BAR_MS = 6 * 3_600_000
PARAMS = TrendParams(long_only=True, target_vol=0.125, top=20)
CANDIDATES = 45                         # like the backtest: 1.5 × 30 most liquid coins are considered
HISTORY = warmup_bars(PARAMS, BAR_MS) + 16
DELAY_MS = 60_000
MAX_HISTORY_POINTS = 4000
MAX_LOG = 500


class TrendPaper:
    def __init__(self, settings: Settings, market: Any, path: Path | None = None) -> None:
        self.settings = settings
        self.market = market
        self.path = path or settings.data_dir / 'trend-paper.json'
        self.error: dict | None = None
        self.state = self._read() or self._fresh()

    # -- persistence -----------------------------------------------------------------
    def _fresh(self) -> dict:
        return dict(capital=self.settings.capital, equity=self.settings.capital, started_at=None, last_bar=None,
                    universe=[], universe_at=0,
                    held={}, prices={}, fills=0, fees=0.0, funding=0.0, btc_start=None, btc_last=None,
                    history=[], log=[])

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

    # -- loop ------------------------------------------------------------------------
    async def run(self) -> None:
        while True:
            now = self.market.now_ms()
            closed = now // BAR_MS * BAR_MS - BAR_MS            # open time of the last closed 6h bar
            if self.state['last_bar'] is None or closed > self.state['last_bar']:
                if now - (closed + BAR_MS) >= DELAY_MS:
                    try:
                        await self.step()
                        self.error = None
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.error = {'at': int(time.time() * 1000), 'message': f'{type(exc).__name__}: {exc}'[:300]}
                        log.warning('Trend portfeli xətası: %s', self.error['message'])
                        await asyncio.sleep(300)
                        continue
            await asyncio.sleep(30)

    async def _klines(self, symbol: str) -> pd.DataFrame | None:
        rows = await self.market._get('/klines', symbol=symbol, interval='6h', limit=HISTORY + 1)
        df = frame(rows)
        if df.empty:
            return None
        df = df[df.index + BAR_MS <= self.market.now_ms()].tail(HISTORY)
        return df if len(df) == HISTORY else None

    async def load(self) -> dict[str, pd.DataFrame]:
        ranked = await self.market.universe()
        symbols = list(dict.fromkeys([s for s, _ in ranked][:CANDIDATES] + list(self.state['held']) + ['BTCUSDT']))
        out: dict[str, pd.DataFrame] = {}
        for i in range(0, len(symbols), 10):
            batch = symbols[i:i + 10]
            frames = await asyncio.gather(*(self._klines(s) for s in batch), return_exceptions=True)
            for s, df in zip(batch, frames):
                if isinstance(df, pd.DataFrame):
                    out[s] = df
        if not out:
            raise RuntimeError('6h şam məlumatı alınmadı')
        last = max(int(df.index[-1]) for df in out.values())
        ref = pd.Index(last - BAR_MS * np.arange(HISTORY)[::-1], name='time')
        return {s: df for s, df in out.items() if df.index.equals(ref)}

    async def step(self) -> None:
        self.apply(await self.load())
        self._save()

    # -- one rebalance ---------------------------------------------------------------
    def apply(self, data: dict[str, pd.DataFrame]) -> None:
        """Mark to market at the last close, then trade toward the new targets (pure; no I/O)."""
        st, p = self.state, PARAMS
        bar = int(next(iter(data.values())).index[-1])
        if st['last_bar'] is not None and bar <= st['last_bar']:
            return
        close = {s: float(df.close.iloc[-1]) for s, df in data.items()}
        ts = bar + BAR_MS
        eq = st['equity']
        held = {s: w for s, w in st['held'].items() if w}
        if held:
            rets = {s: (close[s] / st['prices'][s] - 1) if s in close and st['prices'].get(s) else 0.0 for s in held}
            pnl = eq * sum(w * rets[s] for s, w in held.items())
            hours = (ts - st['history'][-1][0]) / 3_600_000 if st['history'] else 6.0
            fund = eq * p.funding_8h * hours / 8 * sum(max(w, 0) for w in held.values())
            new_eq = eq + pnl - fund
            st['funding'] += fund
            held = {s: w * (1 + rets[s]) * eq / new_eq for s, w in held.items()} if new_eq > 0 else {}
            eq = max(new_eq, 0.0)
        if st['started_at'] is None:
            st['started_at'] = ts
            st['btc_start'] = close.get('BTCUSDT')
        st['btc_last'] = close.get('BTCUSDT', st['btc_last'])
        # universe: top-N by trailing 30-day turnover, re-chosen every 30 days (as in the backtest)
        bars = p.turnover_days * 24 * 3_600_000 // BAR_MS
        if not st.get('universe') or ts - st.get('universe_at', 0) >= p.rotate_days * 86_400_000:
            tv = {s: float((df.close * df.volume).tail(bars).mean()) for s, df in data.items()}
            st['universe'] = sorted(sorted(tv, key=tv.get, reverse=True)[:p.top])
            st['universe_at'] = ts
        members = {s: data[s] for s in st['universe'] if s in data}
        w, _, names = target_weights(members, p, BAR_MS) if members else (np.zeros((1, 0)), None, [])
        target = {s: float(w[-1, j]) for j, s in enumerate(names)}
        for s in held:                                            # a held coin without data is closed
            target.setdefault(s, 0.0)
        trades = []
        for s, tw in sorted(target.items()):
            cur = held.get(s, 0.0)
            if abs(tw - cur) > p.band or (tw == 0 and cur != 0):
                dw = abs(tw - cur)
                cost = eq * p.cost_rate * dw
                eq -= cost
                st['fees'] += cost
                st['fills'] += 1
                trades.append(dict(ts=ts, symbol=s, from_w=round(cur, 4), to_w=round(tw, 4),
                                   price=close.get(s, st['prices'].get(s)), value=round(eq * (tw - cur), 2), fee=round(cost, 4)))
                held[s] = tw
        st['held'] = {s: round(v, 6) for s, v in held.items() if abs(v) > 1e-6}
        st['prices'] = {s: close[s] for s in st['held'] if s in close}
        st['equity'] = eq
        st['last_bar'] = bar
        st['history'].append([ts, round(eq, 2), close.get('BTCUSDT')])
        st['history'] = st['history'][-MAX_HISTORY_POINTS:]
        st['log'] = (st['log'] + trades)[-MAX_LOG:]
        log.info('Trend portfeli: balans %.2f $, %d mövqe, %d əməliyyat', eq, len(st['held']), len(trades))

    # -- API -------------------------------------------------------------------------
    def summary(self) -> dict:
        st = self.state
        cap = st['capital']
        hist = np.array([h[1] for h in st['history']]) if st['history'] else np.array([cap])
        peak = np.maximum.accumulate(np.concatenate([[cap], hist]))
        dd = float((1 - np.concatenate([[cap], hist]) / peak).max() * 100)
        btc = (st['btc_last'] / st['btc_start'] - 1) * 100 if st['btc_start'] and st['btc_last'] else None
        positions = sorted(({'symbol': s, 'weight': w, 'value': w * st['equity'], 'price': st['prices'].get(s)}
                            for s, w in st['held'].items()), key=lambda x: -abs(x['weight']))
        btc_curve = ([[h[0], cap * h[2] / st['btc_start']] for h in st['history'] if len(h) > 2 and h[2]]
                     if st['btc_start'] else [])
        return dict(
            name='Trend portfeli (yalnız LONG, 6h, yarım ölçü)', capital=cap, equity=st['equity'],
            return_pct=(st['equity'] / cap - 1) * 100, max_dd_pct=dd, started_at=st['started_at'],
            last_bar=st['last_bar'], next_run=((st['last_bar'] + 2 * BAR_MS) if st['last_bar'] is not None else None),
            fills=st['fills'], fees=st['fees'], funding=st['funding'], btc_return_pct=btc,
            gross=sum(abs(p['weight']) for p in positions), positions=positions,
            trades=list(reversed(st['log'][-100:])), curve=[h[:2] for h in st['history']], universe=st.get('universe', []), btc_curve=btc_curve, error=self.error,
            params=dict(lookbacks_days=PARAMS.lookbacks_days, target_vol=PARAMS.target_vol, top=PARAMS.top,
                        max_weight=PARAMS.max_weight, max_gross=PARAMS.max_gross, band=PARAMS.band),
        )
