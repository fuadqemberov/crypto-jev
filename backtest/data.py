"""Historical 1h OHLCV loader for every Binance spot USDT coin.

api.binance.com is geo-blocked from some regions (HTTP 451), so the official public
market-data mirror ``data-api.binance.vision`` is used. Responses are cached on disk.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

BASE_URL = 'https://data-api.binance.vision/api/v3'
HOUR_MS = 3_600_000
# Pegged assets: stablecoins, fiat, wrapped/staked duplicates and gold — not tradable "coins".
EXCLUDED_BASES = {
    'USDC', 'FDUSD', 'TUSD', 'BUSD', 'USDP', 'DAI', 'BFUSD', 'RLUSD', 'USD1', 'USDE', 'FRAX', 'EUR', 'EURI',
    'AEUR', 'USDS', 'KGST', 'GBP', 'TRY', 'BRL', 'PAXG', 'XAUT', 'WBTC', 'WBETH', 'BNSOL', 'PYUSD', 'U', 'XUSD',
}


class DataError(Exception):
    pass


class BinanceData:
    def __init__(self, cache_dir: str | Path, workers: int = 5) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.workers = workers
        self.client = httpx.Client(timeout=30)

    def _get(self, path: str, **params):
        for attempt in range(5):
            try:
                r = self.client.get(BASE_URL + path, params=params)
                if r.status_code in (418, 429):
                    delay = min(float(r.headers.get('Retry-After', 60)), 300)
                    log.warning('Rate limit, %ss gözlənilir', delay)
                    time.sleep(delay)
                    continue
                r.raise_for_status()
                if int(r.headers.get('x-mbx-used-weight-1m', 0)) > 4500:
                    time.sleep(60 - time.time() % 60 + 1)
                return r.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning('%s %s xətası (cəhd %d): %s', path, params.get('symbol', ''), attempt + 1, exc)
                time.sleep(2 * (attempt + 1))
        raise DataError(f'{path} {params} alınmadı')

    def symbols(self) -> list[str]:
        info = self._get('/exchangeInfo', permissions='SPOT')
        return sorted(s['symbol'] for s in info['symbols']
                      if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING'
                      and s.get('isSpotTradingAllowed', True) and s['baseAsset'] not in EXCLUDED_BASES
                      and not s['baseAsset'].endswith(('UP', 'DOWN', 'BULL', 'BEAR')))

    def klines(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        """Closed 1h candles with open time in [start_ms, end_ms)."""
        path = self.cache_dir / f'{symbol}_{start_ms}_{end_ms}.pkl'
        if path.exists():
            return pd.read_pickle(path)
        rows, cursor = [], start_ms
        while cursor < end_ms:
            batch = self._get('/klines', symbol=symbol, interval='1h', startTime=cursor, endTime=end_ms - 1, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            cursor = int(batch[-1][0]) + HOUR_MS
            if len(batch) < 1000:
                break
        df = pd.DataFrame([r[:6] for r in rows], columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        if not df.empty:
            df = df.astype({'time': 'int64', 'open': float, 'high': float, 'low': float, 'close': float, 'volume': float})
            df = df.drop_duplicates('time').sort_values('time')
            df = df[(df.time >= start_ms) & (df.time < end_ms)].set_index('time')
        df.to_pickle(path)
        return df

    def load_all(self, start_ms: int, end_ms: int, min_coverage: float = 0.97,
                 symbols: list[str] | None = None) -> tuple[dict[str, pd.DataFrame], list[str]]:
        """Fetch every symbol; keep only coins that existed for (almost) the whole period."""
        symbols = symbols or self.symbols()
        expected = (end_ms - start_ms) // HOUR_MS
        index = pd.Index(np.arange(start_ms, end_ms, HOUR_MS), name='time')
        out, skipped = {}, []
        with ThreadPoolExecutor(self.workers) as pool:
            futures = {pool.submit(self.klines, s, start_ms, end_ms): s for s in symbols}
            for n, fut in enumerate(as_completed(futures), 1):
                sym = futures[fut]
                try:
                    df = fut.result()
                except DataError as exc:
                    skipped.append(sym)
                    log.warning('%s buraxıldı: %s', sym, exc)
                    continue
                if len(df) < expected * min_coverage or df.empty or df.index[0] > start_ms + 24 * HOUR_MS:
                    skipped.append(sym)
                    continue
                df = df.reindex(index)
                # Missing hours (exchange maintenance): flat candle at previous close, zero volume.
                df['close'] = df['close'].ffill().bfill()
                for col in ('open', 'high', 'low'):
                    df[col] = df[col].fillna(df['close'])
                df['volume'] = df['volume'].fillna(0.0)
                out[sym] = df
                if n % 50 == 0:
                    log.info('%d/%d simvol yükləndi', n, len(symbols))
        log.info('%d coin yükləndi, %d coin kifayət qədər tarixçə olmadığı üçün buraxıldı', len(out), len(skipped))
        return dict(sorted(out.items())), sorted(skipped)
