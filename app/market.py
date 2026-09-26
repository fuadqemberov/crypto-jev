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
        # Closed candles only change when a new bar closes; reuse them until then.
        self.klines = {}
        # Bulk quote/mark/server-time snapshot shared by every symbol in a scan.
        self.quotes = None
        self.quotes_lock = asyncio.Lock()

    async def get(self, path, **params):
        # The lock only spaces request starts; responses are awaited outside it so
        # network latency no longer serializes the whole scan.
        async with self.lock:
            now = time.monotonic()
            if now < self.cooldown_until:
                raise MarketError('Binance sorğu limiti: növbəti skanı gözləyin.')
            await asyncio.sleep(max(0, self.next_request - now))
            self.next_request = time.monotonic() + .1
        try:
            r = await self.client.get('https://fapi.binance.com' + path, params=params)
            try:
                # Binance allows 2400 weight/min per IP; back off well before it.
                if int(r.headers.get('X-MBX-USED-WEIGHT-1M', '0')) > 1800:
                    self.next_request = max(self.next_request, time.monotonic() + 60 - time.time() % 60)
            except ValueError:
                pass
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

    async def _quotes(self):
        # Three bulk requests replace three per-symbol requests; refreshed every 2 s.
        async with self.quotes_lock:
            if self.quotes is None or time.monotonic() - self.quotes[0] > 2:
                books = await self.get('/fapi/v1/ticker/bookTicker')
                premiums = await self.get('/fapi/v1/premiumIndex')
                server = int((await self.get('/fapi/v1/time'))['serverTime'])
                self.quotes = (time.monotonic(), server - int(time.time() * 1000),
                               {q['symbol']: q for q in books}, {p['symbol']: p for p in premiums})
            return self.quotes

    async def _frame(self, symbol, interval, now):
        # Indicators use closed bars only, so they are recomputed only after the
        # still-open candle closes. Caching features (not raw bars) keeps memory small.
        cached = self.klines.get((symbol, interval))
        if cached is None or now > cached[0]:
            raw = await self.get('/fapi/v1/klines', symbol=symbol, interval=interval, limit=301)
            bars = closed_bars(raw, interval, now)
            cached = (int(raw[-1][6]) if int(raw[-1][6]) >= now else now, features(bars),
                      bars[-80:] if interval == '15m' else None)
            self.klines[(symbol, interval)] = cached
        elif now - cached[1]['close_time'] > INTERVALS[interval] + 30_000:
            raise MarketError('Bazar məlumatı köhnədir.')
        return cached

    async def snapshot(self, symbol):
        _, offset, books, premiums = await self._quotes()
        if symbol not in books or symbol not in premiums:
            raise MarketError('Bid/ask və ya mark qiyməti yoxdur.')
        quote, premium = books[symbol], premiums[symbol]
        now = int(time.time() * 1000) + offset
        frames = dict(zip(INTERVALS, await asyncio.gather(*(self._frame(symbol, i, now) for i in INTERVALS))))
        bid, ask, mark, funding = float(quote['bidPrice']), float(quote['askPrice']), float(premium['markPrice']), float(premium['lastFundingRate'])
        if not all(math.isfinite(x) for x in (bid, ask, mark, funding)) or not 0 < bid <= ask or mark <= 0:
            raise MarketError('Bazar qiyməti etibarsızdır.')
        if any(not -5000 <= now - int(item['time']) <= 60_000 for item in (quote, premium)):
            raise MarketError('Bid/ask və ya mark qiyməti köhnədir.')
        return dict(symbol=symbol, source='Binance USD-M', observed_at=now, bid=bid, ask=ask, mark=mark,
                    spread_bps=(ask - bid) / ((ask + bid) / 2) * 10000, funding_rate=funding,
                    frames={k: dict(v[1]) for k, v in frames.items()}, candles=frames['15m'][2])


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
