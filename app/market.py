import asyncio
import math
import time
import httpx
from .indicators import INTERVALS, closed_bars, features


class MarketError(Exception):
    pass


class Market:
    def __init__(self, client):
        self.client = client
        self.next_request = 0.
        self.cooldown_until = 0.
        self.lock = asyncio.Lock()

    async def get(self, path, **params):
        async with self.lock:
            now = time.monotonic()
            if now < self.cooldown_until:
                raise MarketError('Binance sorğu limiti: növbəti skanı gözləyin.')
            await asyncio.sleep(max(0, self.next_request - now))
            self.next_request = time.monotonic() + .4
            try:
                r = await self.client.get('https://fapi.binance.com' + path, params=params)
                if r.status_code in (418, 429):
                    try:
                        delay = float(r.headers.get('Retry-After', '300'))
                        if not math.isfinite(delay):
                            delay = 300
                    except ValueError:
                        delay = 300
                    self.cooldown_until = time.monotonic() + max(60, min(delay, 86400))
                    raise MarketError('Binance sorğu limiti tətbiq etdi.')
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as e:
                raise MarketError('Binance məlumatını almaq mümkün olmadı.') from e

    async def discover_symbols(self):
        data = await self.get('/fapi/v1/exchangeInfo')
        symbols = sorted({item['symbol'] for item in data['symbols']
                          if item.get('status') == 'TRADING'
                          and item.get('contractType') == 'PERPETUAL'
                          and item.get('quoteAsset') == 'USDT'
                          and item.get('marginAsset') == 'USDT'
                          and isinstance(item.get('symbol'), str)
                          and item['symbol'].isalnum() and item['symbol'].endswith('USDT')})
        if not symbols:
            raise MarketError('Aktiv USDT perpetual bazarları tapılmadı.')
        return tuple(symbols)

    async def snapshot(self, symbol):
        raw = {}
        for interval in INTERVALS:
            raw[interval] = await self.get('/fapi/v1/klines', symbol=symbol, interval=interval, limit=301)
        quote = await self.get('/fapi/v1/ticker/bookTicker', symbol=symbol)
        premium = await self.get('/fapi/v1/premiumIndex', symbol=symbol)
        now = int((await self.get('/fapi/v1/time'))['serverTime'])
        bars = {k: closed_bars(v, k, now) for k, v in raw.items()}
        bid, ask, mark, funding = float(quote['bidPrice']), float(quote['askPrice']), float(premium['markPrice']), float(premium['lastFundingRate'])
        if not all(math.isfinite(x) for x in (bid, ask, mark, funding)) or not 0 < bid <= ask or mark <= 0:
            raise MarketError('Bazar qiyməti etibarsızdır.')
        if any(not -5000 <= now - int(item['time']) <= 60_000 for item in (quote, premium)):
            raise MarketError('Bid/ask və ya mark qiyməti köhnədir.')
        return dict(symbol=symbol, source='Binance USD-M', observed_at=now, bid=bid, ask=ask, mark=mark,
                    spread_bps=(ask - bid) / ((ask + bid) / 2) * 10000, funding_rate=funding,
                    frames={k: features(v) for k, v in bars.items()}, candles=bars['15m'][-80:])


def demo_snapshot(symbol):
    # Stable synthetic sample, explicitly labelled and never sent to Jev.
    now = int(time.time() * 1000)
    raw = {}
    base = {'BTCUSDT': 65000, 'ETHUSDT': 3000, 'SOLUSDT': 140, 'BNBUSDT': 550, 'XRPUSDT': .6}.get(symbol, 100)
    for interval, step in INTERVALS.items():
        end = now // step * step
        rows = []
        for i in range(301):
            t = end - (301 - i) * step
            close = base * (1 + i * .0003 + .012 * math.sin(i / 6))
            opening = close * (1 + .001 * math.sin(i))
            rows.append([t, opening, max(close, opening) * 1.002, min(close, opening) * .998, close, 100 + i % 30, t + step - 1])
        raw[interval] = closed_bars(rows, interval, now)
    price = raw['15m'][-1]['close']
    return dict(symbol=symbol, source='DEMO — sintetik məlumat', observed_at=now, bid=price * .9999, ask=price * 1.0001,
                mark=price, spread_bps=2., funding_rate=.0001, frames={k: features(v) for k, v in raw.items()}, candles=raw['15m'][-80:])
