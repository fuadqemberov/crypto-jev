"""Binance spot public market data (no API key, read-only).

``data-api.binance.vision`` is Binance's official public market-data mirror; ``api.binance.com``
returns HTTP 451 in some regions. Only GET requests to public endpoints are made.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
import pandas as pd

from backtest.data import EXCLUDED_BASES

from .config import Settings

log = logging.getLogger('app.market')
LEVERAGED_SUFFIXES = ('UP', 'DOWN', 'BULL', 'BEAR')


class MarketError(Exception):
    pass


def frame(rows: list[list[Any]]) -> pd.DataFrame:
    df = pd.DataFrame([r[:6] for r in rows], columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    if df.empty:
        return df.set_index('time')
    df = df.astype({'time': 'int64', 'open': float, 'high': float, 'low': float, 'close': float, 'volume': float})
    return df.drop_duplicates('time').sort_values('time').set_index('time')


class BinanceMarket:
    """Async client with retry, Retry-After cooldown and used-weight back-off."""

    demo = False

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self.interval_ms = settings.interval_ms
        self._cooldown_until = 0.0
        self._tradable: tuple[float, set[str]] = (0.0, set())

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    async def _get(self, path: str, **params: Any) -> Any:
        last: Exception | None = None
        for attempt in range(4):
            wait = self._cooldown_until - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                r = await self.client.get(self.settings.binance_url + path, params=params or None)
                if r.status_code in (418, 429):
                    try:
                        delay = float(r.headers.get('Retry-After', '60'))
                    except ValueError:
                        delay = 60.0
                    self._cooldown_until = time.monotonic() + min(max(delay, 1.0), 300.0)
                    log.warning('Binance rate limit (%s), %.0f s fasilə', r.status_code, delay)
                    last = MarketError(f'HTTP {r.status_code}')
                    continue
                r.raise_for_status()
                try:
                    if int(r.headers.get('x-mbx-used-weight-1m', '0')) > 4800:
                        self._cooldown_until = time.monotonic() + 60 - time.time() % 60 + 1
                except ValueError:
                    pass
                return r.json()
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                await asyncio.sleep(min(2 ** attempt, 8))
        raise MarketError(f'{path} alınmadı: {type(last).__name__}')

    async def _tradable_symbols(self) -> set[str]:
        stamp, symbols = self._tradable
        if symbols and time.monotonic() - stamp < 6 * 3600:
            return symbols
        info = await self._get('/exchangeInfo', permissions='SPOT')
        symbols = {s['symbol'] for s in info.get('symbols', [])
                   if s.get('quoteAsset') == 'USDT' and s.get('status') == 'TRADING'
                   and s.get('isSpotTradingAllowed', True) and s.get('baseAsset') not in EXCLUDED_BASES
                   and not str(s.get('baseAsset', '')).endswith(LEVERAGED_SUFFIXES)}
        if not symbols:
            raise MarketError('exchangeInfo boşdur')
        self._tradable = (time.monotonic(), symbols)
        return symbols

    async def universe(self) -> list[tuple[str, float]]:
        """Tradable USDT pairs ranked by 24h quote volume (top N, or the configured list)."""
        tradable = await self._tradable_symbols()
        tickers = await self._get('/ticker/24hr')
        volume = {t['symbol']: float(t.get('quoteVolume') or 0) for t in tickers if t.get('symbol') in tradable}
        if self.settings.symbols != ('ALL',):
            return sorted(((s, volume.get(s, 0.0)) for s in self.settings.symbols if s in tradable),
                          key=lambda x: -x[1])
        return sorted(volume.items(), key=lambda x: -x[1])[:self.settings.top_n]

    async def prices(self) -> dict[str, float]:
        rows = await self._get('/ticker/price')
        out = {}
        for row in rows:
            try:
                price = float(row['price'])
            except (KeyError, TypeError, ValueError):
                continue
            if price > 0:
                out[row['symbol']] = price
        return out

    async def klines(self, symbol: str, limit: int) -> pd.DataFrame:
        """Last ``limit`` CLOSED candles (the still-forming candle is dropped)."""
        rows = await self._get('/klines', symbol=symbol, interval=self.settings.timeframe, limit=min(limit + 1, 1000))
        df = frame(rows)
        if not df.empty:
            df = df[df.index + self.interval_ms <= self.now_ms()]
        return df.tail(limit)
