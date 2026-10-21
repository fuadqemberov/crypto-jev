"""Deterministic synthetic market for DEMO_MODE and tests (no network).

A virtual clock advances one candle every ``demo_candle_seconds`` so all strategies can be
watched trading within minutes. Prices are a pure function of (symbol, bar index): restarting
the process reproduces the same history.
"""
from __future__ import annotations

import time
import zlib

import numpy as np
import pandas as pd

from .config import Settings
from .market import Quote

DEMO_SYMBOLS = ('ALFA', 'BETA', 'GAMMA', 'DELTA', 'EPSILON', 'ZETA', 'ETA', 'THETA', 'IOTAX', 'KAPPA', 'LAMBDA',
                'MU', 'NU', 'XI', 'OMIKRON', 'PI', 'RHO', 'SIGMA', 'TAU', 'UPSILON', 'PHI', 'CHI', 'PSI', 'OMEGA')
ORIGIN_MS = 1_767_225_600_000  # 2026-01-01 00:00 UTC
FUNDING_MS = 8 * 3_600_000


def _noise(i: np.ndarray, seed: int, salt: int) -> np.ndarray:
    """Hash-style deterministic uniform noise in [0, 1)."""
    x = np.sin(i * 12.9898 + seed * 78.233 + salt * 37.719) * 43758.5453
    return x - np.floor(x)


class DemoMarket:
    demo = True

    def __init__(self, settings: Settings, start_ms: int | None = None, clock=time.monotonic) -> None:
        self.settings = settings
        self.interval_ms = settings.interval_ms
        self._clock = clock
        self._t0 = clock()
        base = start_ms if start_ms is not None else int(time.time() * 1000)
        self._v0 = max(base // self.interval_ms * self.interval_ms, ORIGIN_MS + 600 * self.interval_ms)
        self.symbols = [s + 'USDT' for s in DEMO_SYMBOLS][:max(1, min(settings.top_n, len(DEMO_SYMBOLS)))]

    # -- clock -----------------------------------------------------------------
    def now_ms(self) -> int:
        elapsed = self._clock() - self._t0
        return int(self._v0 + elapsed / self.settings.demo_candle_seconds * self.interval_ms)

    def align(self, resume_ms: int) -> None:
        """After a restart continue from the last processed candle instead of jumping back."""
        if resume_ms > self.now_ms():
            self._v0, self._t0 = resume_ms, self._clock()

    # -- synthetic prices ----------------------------------------------------------
    def _seed(self, symbol: str) -> int:
        return zlib.crc32(symbol.encode()) % 997 + 1

    def _bars(self, symbol: str, last_index: int) -> pd.DataFrame:
        cache = self.__dict__.setdefault('_cache', {})
        hit = cache.get((symbol, last_index))
        if hit is None:
            if len(cache) > 4 * len(self.symbols):
                cache.clear()
            hit = cache[(symbol, last_index)] = self._make_bars(symbol, last_index)
        return hit

    def _make_bars(self, symbol: str, last_index: int) -> pd.DataFrame:
        seed = self._seed(symbol)
        first = ORIGIN_MS // self.interval_ms
        i = np.arange(first, last_index + 1, dtype=np.float64)
        u = (_noise(i, seed, 1) + _noise(i, seed, 2) + _noise(i, seed, 3) - 1.5) * 2.0   # ~N(0, 1)
        vol = 0.004 + (seed % 7) * 0.001
        period = 90 + seed % 160
        drift = 0.0025 * np.sin(2 * np.pi * i / period + seed) + 0.0012 * np.sin(2 * np.pi * i / (period * 3.7))
        logp = np.log(0.5 + seed * 0.37) + np.cumsum(drift + vol * u)
        close = np.exp(logp)
        open_ = np.concatenate([[close[0]], close[:-1]])
        wick = vol * (0.3 + _noise(i, seed, 4))
        high = np.maximum(open_, close) * (1 + wick)
        low = np.minimum(open_, close) * (1 - wick * _noise(i, seed, 5))
        volume = (1000 + seed * 13) * (0.5 + _noise(i, seed, 6)) * (1 + 40 * np.abs(drift + vol * u))
        index = pd.Index((i * self.interval_ms).astype('int64'), name='time')
        return pd.DataFrame({'open': open_, 'high': high, 'low': low, 'close': close, 'volume': volume}, index=index)

    async def universe(self) -> list[tuple[str, float]]:
        ranked = sorted(self.symbols, key=lambda s: -self._seed(s))
        return [(s, float(1_000_000 * (len(ranked) - n))) for n, s in enumerate(ranked)]

    async def quotes(self) -> dict[str, Quote]:
        now = self.now_ms()
        current = now // self.interval_ms
        fraction = (now % self.interval_ms) / self.interval_ms
        next_funding = (now // FUNDING_MS + 1) * FUNDING_MS
        out = {}
        for s in self.symbols:
            bar = self._bars(s, current).iloc[-1]
            seed = self._seed(s)
            rate = 0.0001 + 0.0002 * float(np.sin(next_funding / FUNDING_MS * 0.7 + seed))
            out[s] = Quote(float(bar.open * (bar.close / bar.open) ** fraction), rate, next_funding)
        return out

    async def klines(self, symbol: str, limit: int) -> pd.DataFrame:
        if symbol not in self.symbols:
            return pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])
        last_closed = self.now_ms() // self.interval_ms - 1
        return self._bars(symbol, last_closed).tail(limit)
