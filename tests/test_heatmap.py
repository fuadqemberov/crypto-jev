import asyncio
import time

import httpx
import pytest

from app.config import Settings
from app.engine import Engine
from app.heatmap import COINGLASS_URL, HOUR, Heatmap, hourly_rsi, rsi, zone
from app.market import Market, MarketError
from app.store import Store


def candles(closes, now):
    start = (now // HOUR) * HOUR - len(closes) * HOUR  # last bar closed just before now
    return [[start + i*HOUR, c, c, c, c, 1, start + (i+1)*HOUR - 1] for i, c in enumerate(closes)]


def test_rsi_and_zones():
    assert rsi(list(range(1, 40))) == 100
    assert rsi(list(range(40, 1, -1))) == 0
    assert rsi([1.0] * 30) == 50 and rsi([1.0] * 10) is None
    assert [zone(v) for v in (75, 65, 50, 35, 20, None)] == ['Overbought', 'Strong', 'Neutral', 'Weak', 'Oversold', '—']


def test_hourly_rsi_uses_closed_bars_and_complete_4h_groups():
    now = int(time.time()*1000)
    raw = candles([100 + i for i in range(120)], now)
    raw.append([raw[-1][0] + HOUR, 1, 1, 1, 1, 1, now + HOUR])  # still open: must be ignored
    refresh_at, one, four = hourly_rsi(raw, now)
    assert one == 100 and four == 100 and refresh_at == raw[-2][0] + 2*HOUR


def test_selection_needs_1h_4h_agreement_and_skips_15m_extremes():
    heatmap = Heatmap(Settings(heatmap_size=5), None, None)
    heatmap.rows = {
        'AUSDT': dict(rsi_15m=None, rsi_1h=70, rsi_4h=66),   # strongest long
        'BUSDT': dict(rsi_15m=None, rsi_1h=57, rsi_4h=60),   # weaker long
        'CUSDT': dict(rsi_15m=None, rsi_1h=60, rsi_4h=48),   # disagreement
        'DUSDT': dict(rsi_15m=None, rsi_1h=30, rsi_4h=38),   # short
        'EUSDT': dict(rsi_15m=80, rsi_1h=70, rsi_4h=70),     # 15m overbought: strategy would reject
    }
    assert heatmap.select() == ('AUSDT', 'DUSDT', 'BUSDT')
    status = heatmap.status()
    assert status['rows']['DUSDT']['side'] == 'SHORT' and not status['rows']['CUSDT']['selected']


def test_coinglass_maps_symbols_and_validates():
    payload = {'code': '0', 'data': [
        {'symbol': 'BTC', 'rsi_15m': 55, 'rsi_1h': 61, 'rsi_4h': 62},
        {'symbol': 'PEPE', 'rsi_15m': 40, 'rsi_1h': 35, 'rsi_4h': 30},
        {'symbol': 'BAD', 'rsi_1h': 'nan', 'rsi_4h': 50},
        {'symbol': 'NOTLISTED', 'rsi_1h': 60, 'rsi_4h': 60}]}
    seen = {}
    def handler(request):
        seen['key'] = request.headers.get('CG-API-KEY')
        assert str(request.url) == COINGLASS_URL
        return httpx.Response(200, json=payload)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            heatmap = Heatmap(Settings(coinglass_api_key='secret'), client, None)
            await heatmap.refresh(('BTCUSDT', '1000PEPEUSDT', 'BADUSDT'))
            return heatmap
    heatmap = asyncio.run(run())
    assert seen['key'] == 'secret' and heatmap.source == 'CoinGlass'
    assert set(heatmap.rows) == {'BTCUSDT', '1000PEPEUSDT'}


def test_binance_heatmap_caches_until_next_hourly_close():
    calls = []
    now = int(time.time()*1000)
    def handler(request):
        calls.append(request.url.params['symbol'])
        return httpx.Response(200, json=candles([100 + i for i in range(120)], now))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            heatmap = Heatmap(Settings(), client, Market(client))
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


@pytest.mark.parametrize('kwargs', [dict(heatmap_source='x'), dict(heatmap_source='coinglass'),
                                    dict(heatmap_size=1), dict(heatmap_trend_rsi=50)])
def test_heatmap_settings_validated(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)
