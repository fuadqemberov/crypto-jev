"""RSI heatmap radar: picks the markets worth a full analysis instead of scanning every market.

Source: the CoinGlass RSI list (one request; needs COINGLASS_API_KEY, Standard plan or higher) or,
without a key, the same heatmap computed from Binance closed 1h candles (one request per market,
cached until the next 1h close; 4h RSI is aggregated from the same candles).
Only markets whose 1h and 4h RSI agree on a side are selected; the strategy still decides.
"""
from __future__ import annotations
from typing import Any
import asyncio
import logging
import math
import time
import httpx
from .config import Settings
from .indicators import wilder
from .market import Market, MarketError
from .metrics import event

log = logging.getLogger(__name__)
COINGLASS_URL = 'https://open-api-v4.coinglass.com/api/futures/rsi/list'
HOUR, FOUR_HOURS = 3_600_000, 14_400_000


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gain, loss = wilder([max(x, 0) for x in changes], period), wilder([max(-x, 0) for x in changes], period)
    return 50. if gain == loss == 0 else 100. if loss == 0 else 100 - 100 / (1 + gain / loss)


def zone(value: float | None) -> str:
    # CoinGlass heatmap bands.
    if value is None:
        return '—'
    return ('Overbought' if value >= 70 else 'Strong' if value >= 60 else 'Neutral' if value > 40
            else 'Weak' if value > 30 else 'Oversold')


def valid_rsi(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and 0 <= value <= 100 else None


def hourly_rsi(raw: list[list[Any]], now: int) -> tuple[int, float | None, float | None]:
    """Closed 1h bars → (next close time, RSI 1h, RSI 4h from complete UTC-aligned 4h groups)."""
    bars = [(int(r[0]), float(r[4])) for r in raw if int(r[6]) < now]
    if not bars or any(not math.isfinite(c) or c <= 0 for _, c in bars):
        raise ValueError('Invalid candles')
    groups: dict[int, list[float]] = {}
    for start, close in bars:
        groups.setdefault(start // FOUR_HOURS, []).append(close)
    four = [closes[-1] for _, closes in sorted(groups.items()) if len(closes) == 4]
    return bars[-1][0] + 2*HOUR, rsi([c for _, c in bars]), rsi(four)


class Heatmap:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, market: Market) -> None:
        self.settings, self.client, self.market = settings, client, market
        self.rows: dict[str, dict[str, Any]] = {}
        self.cache: dict[str, tuple[int, float | None, float | None]] = {}
        self.source: str | None = None
        self.observed_at: int | None = None
        self.error: str | None = None

    @property
    def use_coinglass(self) -> bool:
        return self.settings.heatmap_source == 'coinglass' or (
            self.settings.heatmap_source == 'auto' and bool(self.settings.coinglass_api_key))

    async def refresh(self, symbols: tuple[str, ...]) -> None:
        try:
            rows = await (self._coinglass(symbols) if self.use_coinglass else self._binance(symbols))
            if len(rows) < max(1, len(symbols) // 2):
                raise MarketError('Heatmap üçün bazarların yarısından azı alındı.')
            self.rows, self.observed_at, self.error = rows, int(time.time()*1000), None
            self.source = 'CoinGlass' if self.use_coinglass else 'Binance 1h/4h'
        except Exception as exc:
            event(log, logging.WARNING, 'heatmap', error=exc)
            self.error = 'RSI heatmap yenilənmədi; radar 24s həcmə görə seçilir.'
            raise

    async def _coinglass(self, symbols: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        try:
            r = await self.client.get(COINGLASS_URL, headers={'CG-API-KEY': self.settings.coinglass_api_key,
                                                              'accept': 'application/json'}, timeout=10)
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketError('CoinGlass RSI məlumatı alınmadı.') from exc
        if str(body.get('code')) != '0' or not isinstance(body.get('data'), list):
            raise MarketError('CoinGlass cavabı qəbul edilmədi.')
        allowed, rows = set(symbols), {}
        for item in body['data']:
            base = item.get('symbol') if isinstance(item, dict) else None
            if not isinstance(base, str) or not base.isalnum():
                continue
            # Binance lists small-price coins as 1000PEPEUSDT etc.
            symbol = next((s for s in (f'{base}USDT', f'1000{base}USDT', f'1000000{base}USDT') if s in allowed), None)
            values = {k: valid_rsi(item.get(k)) for k in ('rsi_15m', 'rsi_1h', 'rsi_4h')}
            if symbol and values['rsi_1h'] is not None and values['rsi_4h'] is not None:
                rows[symbol] = values
        return rows

    async def _binance(self, symbols: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        self.cache = {s: v for s, v in self.cache.items() if s in set(symbols)}
        now = int(time.time()*1000)
        queue = iter([s for s in symbols if s not in self.cache or now >= self.cache[s][0]])

        async def worker() -> None:
            for symbol in queue:
                try:
                    # limit < 500 keeps the Binance weight at 2 per market.
                    raw = await self.market.get('/fapi/v1/klines', symbol=symbol, interval='1h', limit=400)
                    self.cache[symbol] = hourly_rsi(raw, int(time.time()*1000))
                except (MarketError, ValueError, TypeError, IndexError) as exc:
                    event(log, logging.INFO, 'heatmap_symbol', symbol, exc)
        await asyncio.gather(*(worker() for _ in range(8)))
        return {s: dict(rsi_15m=None, rsi_1h=v[1], rsi_4h=v[2]) for s, v in self.cache.items()
                if s in set(symbols) and v[1] is not None and v[2] is not None}

    def side(self, row: dict[str, Any]) -> str | None:
        trend, limit = self.settings.heatmap_trend_rsi, self.settings.rules.rsi_long_max
        fast = row.get('rsi_15m')
        if row['rsi_1h'] >= trend and row['rsi_4h'] >= trend and (fast is None or fast <= limit):
            return 'LONG'
        if row['rsi_1h'] <= 100-trend and row['rsi_4h'] <= 100-trend and (fast is None or fast >= 100-limit):
            return 'SHORT'
        return None

    def select(self) -> tuple[str, ...]:
        """Strongest 1h/4h agreement first; 15m extremes are skipped because the RSI guard would reject them."""
        picked = [(min(abs(r['rsi_1h']-50), abs(r['rsi_4h']-50)), s) for s, r in self.rows.items() if self.side(r)]
        return tuple(s for _, s in sorted(picked, reverse=True)[:self.settings.heatmap_size])

    def status(self) -> dict[str, Any]:
        ranked = self.select()
        selected = set(ranked)
        return dict(source=self.source, observed_at=self.observed_at, error=self.error,
                    trend_rsi=self.settings.heatmap_trend_rsi, size=self.settings.heatmap_size,
                    selected=ranked,
                    rows={s: {**{k: None if v is None else round(v, 1) for k, v in r.items()},
                              'zone_1h': zone(r['rsi_1h']), 'side': self.side(r), 'selected': s in selected}
                          for s, r in self.rows.items()})
