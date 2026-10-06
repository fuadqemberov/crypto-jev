"""RSI + MACD heatmap radar: picks the markets worth a full analysis instead of scanning every market.

Computed from Binance closed 1h candles: one request per market, cached until the next 1h close;
4h values are aggregated from the same candles. A market is selected only when RSI and the MACD
histogram on both 1h and 4h agree on one side. The strategy still makes the entry decision.
"""
from __future__ import annotations
from typing import Any
import asyncio
import logging
import math
import time
from .config import Settings
from .indicators import ema, wilder
from .market import Market, MarketError
from .metrics import event

log = logging.getLogger(__name__)
HOUR, FOUR_HOURS = 3_600_000, 14_400_000
FIELDS = ('rsi_1h', 'rsi_4h', 'macd_1h', 'macd_4h')


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gain, loss = wilder([max(x, 0) for x in changes], period), wilder([max(-x, 0) for x in changes], period)
    return 50. if gain == loss == 0 else 100. if loss == 0 else 100 - 100 / (1 + gain / loss)


def macd_hist(closes: list[float]) -> float | None:
    """MACD(12, 26, 9) histogram, relative to price so markets are comparable."""
    if len(closes) < 26 + 9:
        return None
    line = [f - s for f, s in zip(ema(closes, 12), ema(closes, 26)) if s is not None]
    return (line[-1] - ema(line, 9)[-1]) / closes[-1]


def zone(value: float | None) -> str:
    # CoinGlass RSI heatmap bands.
    if value is None:
        return '—'
    return ('Overbought' if value >= 70 else 'Strong' if value >= 60 else 'Neutral' if value > 40
            else 'Weak' if value > 30 else 'Oversold')


def hourly_values(raw: list[list[Any]], now: int) -> tuple[int, dict[str, float | None]]:
    """Closed 1h bars → (next close time, RSI/MACD on 1h and on complete UTC-aligned 4h groups)."""
    bars = [(int(r[0]), float(r[4])) for r in raw if int(r[6]) < now]
    if not bars or any(not math.isfinite(c) or c <= 0 for _, c in bars):
        raise ValueError('Invalid candles')
    groups: dict[int, list[float]] = {}
    for start, close in bars:
        groups.setdefault(start // FOUR_HOURS, []).append(close)
    hourly = [c for _, c in bars]
    four = [closes[-1] for _, closes in sorted(groups.items()) if len(closes) == 4]
    return bars[-1][0] + 2*HOUR, dict(rsi_1h=rsi(hourly), rsi_4h=rsi(four), macd_1h=macd_hist(hourly), macd_4h=macd_hist(four))


class Heatmap:
    def __init__(self, settings: Settings, market: Market) -> None:
        self.settings, self.market = settings, market
        self.rows: dict[str, dict[str, float]] = {}
        self.cache: dict[str, tuple[int, dict[str, float | None]]] = {}
        self.observed_at: int | None = None
        self.error: str | None = None

    async def refresh(self, symbols: tuple[str, ...]) -> None:
        try:
            allowed = set(symbols)
            self.cache = {s: v for s, v in self.cache.items() if s in allowed}
            now = int(time.time()*1000)
            queue = iter([s for s in symbols if s not in self.cache or now >= self.cache[s][0]])

            async def worker() -> None:
                for symbol in queue:
                    try:
                        # limit < 500 keeps the Binance weight at 2 per market.
                        raw = await self.market.get('/fapi/v1/klines', symbol=symbol, interval='1h', limit=400)
                        self.cache[symbol] = hourly_values(raw, int(time.time()*1000))
                    except (MarketError, ValueError, TypeError, IndexError) as exc:
                        event(log, logging.INFO, 'heatmap_symbol', symbol, exc)
            await asyncio.gather(*(worker() for _ in range(8)))
            rows = {s: v for s, (_, v) in self.cache.items() if all(v[k] is not None for k in FIELDS)}
            if len(rows) < max(1, len(symbols) // 2):
                raise MarketError('Heatmap üçün bazarların yarısından azı alındı.')
            self.rows, self.observed_at, self.error = rows, int(time.time()*1000), None
        except Exception as exc:
            event(log, logging.WARNING, 'heatmap', error=exc)
            self.error = 'RSI/MACD heatmap yenilənmədi; radar 24s həcmə görə seçilir.'
            raise

    def side(self, row: dict[str, float]) -> str | None:
        trend = self.settings.heatmap_trend_rsi
        if row['rsi_1h'] >= trend and row['rsi_4h'] >= trend and row['macd_1h'] > 0 and row['macd_4h'] > 0:
            return 'LONG'
        if row['rsi_1h'] <= 100-trend and row['rsi_4h'] <= 100-trend and row['macd_1h'] < 0 and row['macd_4h'] < 0:
            return 'SHORT'
        return None

    def select(self) -> tuple[str, ...]:
        """Strongest 1h/4h RSI agreement first among markets whose MACD confirms the side."""
        picked = [(min(abs(r['rsi_1h']-50), abs(r['rsi_4h']-50)), s) for s, r in self.rows.items() if self.side(r)]
        return tuple(s for _, s in sorted(picked, reverse=True)[:self.settings.heatmap_size])

    def status(self) -> dict[str, Any]:
        ranked = self.select()
        selected = set(ranked)
        return dict(source='Binance 1h/4h RSI + MACD' if self.observed_at else None, observed_at=self.observed_at, error=self.error,
                    trend_rsi=self.settings.heatmap_trend_rsi, size=self.settings.heatmap_size, selected=ranked,
                    rows={s: dict(rsi_1h=round(r['rsi_1h'], 1), rsi_4h=round(r['rsi_4h'], 1),
                                  macd_1h=round(r['macd_1h'], 6), macd_4h=round(r['macd_4h'], 6), zone_1h=zone(r['rsi_1h']),
                                  side=self.side(r), selected=s in selected)
                          for s, r in self.rows.items()})
