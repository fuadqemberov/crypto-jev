import asyncio
import time

import httpx
import pytest

from app.config import Settings
from app.engine import Engine
from app.heatmap import HOUR, Heatmap, hourly_values, macd_hist, rsi, zone
from app.market import Market, MarketError
from app.store import Store


def candles(closes, now):
    start = (now // HOUR) * HOUR - len(closes) * HOUR  # last bar closed just before now
    return [[start + i*HOUR, c, c, c, c, 1, start + (i+1)*HOUR - 1] for i, c in enumerate(closes)]


def test_rsi_macd_and_zones():
    assert rsi(list(range(1, 40))) == 100
    assert rsi(list(range(40, 1, -1))) == 0
    assert rsi([1.0] * 30) == 50 and rsi([1.0] * 10) is None
    rally, selloff = [100.]*50 + [100 + 2*i for i in range(10)], [100.]*50 + [100 - 2*i for i in range(10)]
    assert macd_hist(rally) > 0 > macd_hist(selloff)
    assert macd_hist([1.0] * 20) is None
    assert [zone(v) for v in (75, 65, 50, 35, 20, None)] == ['Overbought', 'Strong', 'Neutral', 'Weak', 'Oversold', '—']


def test_hourly_values_use_closed_bars_and_complete_4h_groups():
    now = int(time.time()*1000)
    raw = candles([100.]*180 + [100 + i for i in range(20)], now)
    raw.append([raw[-1][0] + HOUR, 1, 1, 1, 1, 1, now + HOUR])  # still open: must be ignored
    refresh_at, values = hourly_values(raw, now)
    assert refresh_at == raw[-2][0] + 2*HOUR
    assert values['rsi_1h'] == values['rsi_4h'] == 100 and values['macd_1h'] > 0 and values['macd_4h'] > 0


def row(r1, r4, m1=.001, m4=.001):
    return dict(rsi_1h=r1, rsi_4h=r4, macd_1h=m1, macd_4h=m4)


def test_selection_needs_rsi_and_macd_agreement_on_1h_and_4h():
    heatmap = Heatmap(Settings(heatmap_size=5), None)
    heatmap.rows = {
        'AUSDT': row(70, 66),              # strongest long
        'BUSDT': row(57, 60),              # weaker long
        'CUSDT': row(60, 48),              # RSI disagreement
        'DUSDT': row(30, 38, -.001, -.002),  # short
        'EUSDT': row(75, 75, .001, -.001),   # 4h MACD against the side
    }
    assert heatmap.select() == ('AUSDT', 'DUSDT', 'BUSDT')
    status = heatmap.status()
    assert status['rows']['DUSDT']['side'] == 'SHORT' and not status['rows']['EUSDT']['selected']


def test_heatmap_caches_until_next_hourly_close():
    calls = []
    now = int(time.time()*1000)
    def handler(request):
        calls.append(request.url.params['symbol'])
        return httpx.Response(200, json=candles([100.]*180 + [100 + i for i in range(20)], now))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            heatmap = Heatmap(Settings(), Market(client))
            await heatmap.refresh(('BTCUSDT', 'ETHUSDT'))
            await heatmap.refresh(('BTCUSDT', 'ETHUSDT'))
            return heatmap
    heatmap = asyncio.run(run())
    # The second refresh reuses the cache: one request per market until the next 1h close.
    assert sorted(calls) == ['BTCUSDT', 'ETHUSDT'] and set(heatmap.select()) == {'BTCUSDT', 'ETHUSDT'}


def test_radar_falls_back_to_volume_and_keeps_open_positions(tmp_path):
    async def run():
        store = Store(tmp_path/'radar.db')
        engine = Engine(Settings(symbols=('ALL',), heatmap_size=5), None, store)
        engine.symbols = tuple(f'C{i}USDT' for i in range(20))
        async def broken(symbols): raise MarketError('down')
        async def ranking(symbols): return symbols[::-1]
        engine.heatmap.refresh = broken
        engine.market.volume_ranking = ranking
        engine._open_symbols = lambda: ['OPENUSDT']
        selected = await engine._radar_selection()
        store.close()
        return selected
    assert asyncio.run(run()) == ('OPENUSDT', 'C19USDT', 'C18USDT', 'C17USDT', 'C16USDT', 'C15USDT')


@pytest.mark.parametrize('kwargs', [dict(heatmap_size=1), dict(heatmap_trend_rsi=50)])
def test_heatmap_settings_validated(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)
