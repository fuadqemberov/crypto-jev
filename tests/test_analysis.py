import asyncio
import math
import time
import httpx
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.indicators import closed_bars, features, ema
from app.market import demo_snapshot, Market, MarketError
from app.strategy import research_levels
from app.store import Store
from app.engine import Engine
from app.main import create_app




def raw_bars(n=301):
    step = 900000
    end = int(time.time() * 1000) // step * step
    return [[end - (n - i) * step, 100, 101, 99, 100, 10, end - (n - i - 1) * step - 1] for i in range(n)]


def test_flat_indicators_are_finite():
    f = features(closed_bars(raw_bars(), '15m', int(time.time() * 1000)))
    assert f['rsi'] == 50 and f['macd_hist'] == 0 and f['atr'] == 2
    assert f['ema200'] == 100 and f['support'] is None
    assert ema([1, 2, 3, 4], 3) == [None, None, 2, 3]


def test_open_bar_excluded_and_gaps_rejected():
    rows = raw_bars(); now = rows[-1][6] - 100
    assert len(closed_bars(rows, '15m', now)) == 300
    with pytest.raises(ValueError): closed_bars(rows[:100] + rows[101:], '15m', now)
    with pytest.raises(ValueError): closed_bars(rows, '15m', rows[-1][6] + 1800000)


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -1, 0])
def test_bad_prices_rejected(bad):
    rows = raw_bars(); rows[-1][4] = bad
    with pytest.raises(ValueError): closed_bars(rows, '15m', rows[-1][6] + 1)


def test_rsi_monotonic():
    rows = raw_bars()
    for i, r in enumerate(rows): r[1:5] = [100+i, 101+i, 99+i, 100+i]
    assert features(closed_bars(rows, '15m', rows[-1][6]+1))['rsi'] == 100








def test_binance_rate_limit_shared_cooldown():
    async def run():
        requests=[]
        def handler(r):
            requests.append(r)
            return httpx.Response(429, headers={'Retry-After':'600'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            m=Market(c)
            for _ in range(2):
                with pytest.raises(MarketError): await m.get('/fapi/v1/time')
            assert len(requests)==1
    asyncio.run(run())


def test_levels_long_short_and_costs():
    snap=demo_snapshot('BTCUSDT')
    for direction in ('LONG','SHORT'):
        l=research_levels(snap,direction)
        assert l['net_rr'] < l['gross_rr'] == 2
        if direction=='LONG': assert l['stop']<l['entry']<l['target']
        else: assert l['target']<l['entry']<l['stop']


def test_store_restart_and_demo_engine(tmp_path):
    path=tmp_path/'test.db'; store=Store(path)
    async def run():
        async with httpx.AsyncClient() as c:
            e=Engine(Settings(demo=True,symbols=('BTCUSDT',)),c,store)
            await e.scan()
            assert e.status()['rows'][0]['decision']=='WAIT'
            e.rows['BTCUSDT']['observed_at']=0
            assert e.status()['rows'][0]['stale']
    asyncio.run(run()); store.close()
    store=Store(path)
    assert store.history()[0]['symbol']=='BTCUSDT'
    store.close()


def test_market_error_clears_old_signal(tmp_path):
    async def run():
        store=Store(tmp_path/'e.db')
        async with httpx.AsyncClient() as c:
            e=Engine(Settings(symbols=('BTCUSDT',)),c,store)
            async def market(symbol): return demo_snapshot(symbol)
            e.market.snapshot=market
            await e.scan()
            assert e.rows['BTCUSDT']['decision']=='WAIT'
            async def fail(symbol): raise ValueError('network')
            e.rows['BTCUSDT']['decision']='LONG';e.market.snapshot=fail
            await e.scan()
            assert e.rows['BTCUSDT']['decision']=='WAIT' and 'frames' not in e.rows['BTCUSDT']
        store.close()
    asyncio.run(run())




def test_auth_static_and_csrf(tmp_path):
    settings=Settings(data_dir=tmp_path,demo=True,user='fuad',password='example-password')
    with TestClient(create_app(settings,start_worker=False)) as client:
        assert client.get('/api/status').status_code==401
        client.auth=('fuad','example-password')
        assert client.get('/').status_code==200
        assert client.get('/static/app.js').status_code==200
        assert client.get('/api/status').json()['rows']==[]
        assert client.post('/api/scan').status_code==403
        assert client.post('/api/scan',headers={'X-Crypto-Radar':'1','Sec-Fetch-Site':'cross-site'}).status_code==403
        assert client.post('/api/scan',headers={'X-Crypto-Radar':'1'}).status_code==202
        assert client.get('/api/status').headers['Cache-Control']=='no-store'


def test_configuration_validation():
    with pytest.raises(ValueError): Settings(scan_seconds=59)
    with pytest.raises(ValueError): Settings(max_spread=math.nan)
    with pytest.raises(ValueError): Settings(symbols=('../../secret',))
    with pytest.raises(ValueError): Settings(user='fuad')
