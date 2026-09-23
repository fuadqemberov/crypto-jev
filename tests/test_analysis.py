import asyncio
import math
import time
import httpx
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.indicators import closed_bars, features, ema
from app.market import demo_snapshot, Market, MarketError
from app.jev import parse_response, Jev, JevError, QUESTIONS
from app.strategy import decide, research_levels
from app.store import Store
from app.engine import Engine
from app.main import create_app


def answer(direction='LONG'):
    chosen = {'leverage': '1', 'direction': direction, 'momentum': 'bullish' if direction == 'LONG' else 'bearish', 'regime': 'trend', 'risk': 'acceptable', 'driver': 'trend_alignment'}
    return dict(model='jev-test', answers={k: dict(type='choice', choice=chosen[k], confidence=.95,
                probabilities={v: 1. if v == chosen[k] else 0. for v in q['criteria']}) for k, q in QUESTIONS.items()})


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


@pytest.mark.parametrize('direction', ['LONG', 'SHORT'])
def test_jev_is_primary_even_with_low_technical_score(direction):
    tech = dict(candidate=direction, score=85, guards=[dict(label='spread', passed=True)])
    s = Settings()
    assert decide(tech, answer(direction), s)[0] == direction
    assert decide({**tech, 'score': 10}, answer(direction), s)[0] == direction
    assert decide(tech, None, s)[0] == 'WAIT'
    ai = answer(direction); ai['answers']['risk']['confidence'] = .79
    assert decide(tech, ai, s)[0] == 'WAIT'
    tech['guards'][0]['passed'] = False
    assert decide(tech, answer(direction), s)[0] == 'WAIT'


@pytest.mark.parametrize('mutation', ['missing', 'nan', 'sum', 'choice', 'confidence', 'bool'])
def test_jev_invalid_response_fails_closed(mutation):
    a = answer(); v = a['answers']['direction']
    if mutation == 'missing': del a['answers']['risk']
    if mutation == 'nan': v['probabilities']['LONG'] = math.nan
    if mutation == 'sum': v['probabilities']['LONG'] = .2
    if mutation == 'choice': v['choice'] = 'SHORT'
    if mutation == 'confidence': v['confidence'] = 2
    if mutation == 'bool': v['confidence'] = True
    with pytest.raises(JevError): parse_response(a)


def test_jev_http_contract_and_secret_not_in_error():
    async def run():
        seen=[]
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=answer())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await Jev(client, Settings(api_key='test-only-secret')).evaluate({'trend': 'bullish'})
            assert result['model'] == 'jev-test'
            assert str(seen[0].url) == 'https://api.typesafe.ai/v1/systemone'
            assert seen[0].headers['Authorization'] == 'Bearer test-only-secret'
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, text='test-only-secret'))) as client:
            with pytest.raises(JevError) as e: await Jev(client, Settings(api_key='test-only-secret')).evaluate({})
            assert 'test-only-secret' not in str(e.value)
    asyncio.run(run())


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
            assert e.status()['rows'][0]['ai'] is None
            e.rows['BTCUSDT']['observed_at']=0
            assert e.status()['rows'][0]['stale']
    asyncio.run(run()); store.close()
    store=Store(path)
    assert store.history()[0]['symbol']=='BTCUSDT'
    store.close()


def test_no_key_means_no_ai_calls_and_errors_clear_old_signal(tmp_path):
    async def run():
        store=Store(tmp_path/'e.db')
        async with httpx.AsyncClient() as c:
            e=Engine(Settings(symbols=('BTCUSDT',)),c,store)
            async def market(symbol): return demo_snapshot(symbol)
            async def unexpected(state): raise AssertionError('No key must never call Jev')
            e.market.snapshot=market; e.jev.evaluate=unexpected
            await e.scan()
            assert e.rows['BTCUSDT']['ai'] is None and e.rows['BTCUSDT']['decision']=='WAIT'
            async def fail(symbol): raise ValueError('network')
            e.rows['BTCUSDT']['decision']='LONG';e.market.snapshot=fail
            await e.scan()
            assert e.rows['BTCUSDT']['decision']=='WAIT' and 'frames' not in e.rows['BTCUSDT']
        store.close()
    asyncio.run(run())


def test_ai_cache_avoids_duplicate_requests(tmp_path):
    async def run():
        store=Store(tmp_path/'cache.db');calls=[]
        async with httpx.AsyncClient() as c:
            e=Engine(Settings(api_key='test',symbols=('BTCUSDT',)),c,store)
            snap=demo_snapshot('BTCUSDT')
            async def market(symbol): return snap
            async def ai(state): calls.append(state);return answer()
            e.market.snapshot=market;e.jev.evaluate=ai
            await e.scan();await e.scan()
            assert len(calls)==1
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
        assert client.post('/api/scan',headers={'X-Crypto-Jev':'1','Sec-Fetch-Site':'cross-site'}).status_code==403
        assert client.post('/api/scan',headers={'X-Crypto-Jev':'1'}).status_code==202
        assert client.get('/api/status').headers['Cache-Control']=='no-store'


def test_configuration_validation():
    with pytest.raises(ValueError): Settings(scan_seconds=59)
    with pytest.raises(ValueError): Settings(min_confidence=math.nan)
    with pytest.raises(ValueError): Settings(symbols=('../../secret',))
    with pytest.raises(ValueError): Settings(user='fuad')
