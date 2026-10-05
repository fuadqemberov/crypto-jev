import asyncio
from copy import deepcopy
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from app.bridge import VERSION, fresh, validate_payload, signal_error
from app.cache import AnswerCache
from app.jev import JevError
from app.config import Settings
from app.engine import Engine
from app.execution import Execution
from app.main import create_app
from app.market import demo_snapshot
from app.risk import RiskPolicy
from app.store import Store
from test_analysis import answer
from test_execution import connected, row


def heartbeat(settings):
    return dict(version=VERSION, mode='dry_run', risk_policy=settings.risk.identity,
        generated_at=int(time.time()*1000), risk_ready=True, daily_loss_hit=False,
        equity=2000., realized_today=0., unrealized=0., cooldowns={}, rejections={}, bridge_lag_ms=1.)


def test_cache_lru_ttl_and_restart(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr('app.cache.time.time', lambda: now)
    store = Store(tmp_path/'cache.db')
    cache = AnswerCache(2, 60, store)
    cache.put('a', answer()); cache.put('b', answer())
    cache.get('a'); cache.put('c', answer())
    assert list(cache.items) == ['a', 'c'] and cache.evictions == 1
    restarted = AnswerCache(2, 60, store)
    assert restarted.get('c')['answers']['direction']['choice'] == 'LONG'
    assert restarted.get('different-prompt-version') is None
    now += 60
    assert restarted.get('c') is None
    now -= 120
    assert cache.get('a') is None  # Future-created answers are rejected after clock rollback.
    store.close()


def test_corrupt_disk_cache_is_not_used(tmp_path):
    store = Store(tmp_path/'cache.db')
    store.cache_put('key', (time.time(), {'answers': {}}), 10, 300)
    with pytest.raises(JevError):
        AnswerCache(10, 300, store).get('key')
    store.close()


def test_heartbeat_auth_validation_and_expiry(tmp_path):
    settings = Settings(data_dir=tmp_path, bridge_token='x'*32)
    with TestClient(create_app(settings, start_worker=False)) as client:
        value = heartbeat(settings)
        assert client.post('/api/execution/heartbeat', json=value).status_code == 401
        headers = {'Authorization': 'Bearer '+'x'*32}
        assert client.post('/api/execution/heartbeat', json=value, headers=headers).status_code == 200
        execution = client.app.state.engine.execution
        connected(execution)
        assert client.get('/api/execution/health', headers=headers).status_code == 200
        execution.heartbeat['generated_at'] -= 16000
        assert 'heartbeat' in execution.signals([row()])['diagnostics']['blocks']
        assert client.get('/api/execution/health', headers=headers).status_code == 503
        for mutation in [dict(version=2), dict(risk_policy='wrong'), dict(generated_at=0), dict(equity='bad'),
                         dict(rejections={'secret':1}), dict(cooldowns={'invalid':123})]:
            assert client.post('/api/execution/heartbeat', json={**value, **mutation}, headers=headers).status_code == 422
        assert client.post('/api/execution/heartbeat', content=b'x'*256001, headers=headers).status_code == 413


def test_storage_read_failure_blocks_entries_and_close(tmp_path):
    store = Store(tmp_path/'storage.db')
    execution = Execution(Settings(), None, store)
    connected(execution)
    value = row(); value['position_id'] = 7
    value['ai']['answers']['position_action'] = {'choice':'CLOSE', 'confidence':.99}
    store.close()
    payload = execution.signals([value])
    assert not payload['entries_enabled'] and 'storage_error' in payload['diagnostics']['blocks']
    assert payload['signals'][0]['action'] == 'WAIT'
    assert 'close_trade_id' not in payload['signals'][0]


def test_bridge_strict_types_and_freshness():
    now = int(time.time()*1000)
    policy = RiskPolicy()
    base = dict(version=VERSION, mode='dry_run', risk_policy=policy.identity,
                generated_at=now, entries_enabled=True, signals=[])
    for mutation in [dict(version=2), dict(version=True), dict(mode='live'), dict(entries_enabled=1),
                     dict(generated_at=now+1), dict(generated_at=now-15001), dict(signals={})]:
        with pytest.raises(ValueError): validate_payload({**base, **mutation}, now, policy.identity)
    validate_payload(base, now, policy.identity)
    value = dict(id='a'*24, pair='BTC/USDT:USDT', action='LONG', leverage_requested=5, funding_cost=.0001,
                 observed_at=now, expires_at=now+120000, levels=dict(entry=100, stop=98, target=104))
    assert signal_error(value, now) is None
    for mutation in [dict(funding_cost=float('nan')), dict(close_trade_id=True), dict(leverage_requested=True),
                     dict(pair='../../evil'), dict(expires_at=now+300001), dict(observed_at=now+1)]:
        assert signal_error({**value, **mutation}, now)
    assert not fresh(value, value['expires_at'])


def test_priority_progresses_while_large_radar_is_blocked(tmp_path):
    async def run():
        store = Store(tmp_path/'engine.db')
        symbols = tuple(f'C{i}USDT' for i in range(500))
        engine = Engine(Settings(symbols=symbols, radar_parallelism=2, priority_parallelism=1), None, store)
        release = asyncio.Event(); entered = asyncio.Event()
        async def analyse(symbol):
            if symbol != 'BTCUSDT':
                entered.set()
                await release.wait()
            return dict(symbol=symbol, observed_at=int(time.time()*1000), decision='WAIT')
        engine._analyse = analyse
        radar = asyncio.create_task(engine._batch(symbols, 2, 'radar'))
        await entered.wait()
        start = time.monotonic()
        await asyncio.wait_for(engine._batch(('BTCUSDT',), 1, 'priority'), .5)
        assert 'BTCUSDT' in engine.rows and not radar.done()
        assert len(engine.inflight) == 2
        print(f'500-symbol blocked radar: priority published in {(time.monotonic()-start)*1000:.2f} ms')
        release.set(); await radar
        assert len(engine.rows) == 501
        store.close()
    asyncio.run(run())


def test_same_symbol_is_single_flight_and_cancel_releases_slot(tmp_path):
    async def run():
        store = Store(tmp_path/'engine.db'); engine = Engine(Settings(), None, store)
        entered = asyncio.Event()
        async def analyse(symbol):
            entered.set(); await asyncio.Event().wait()
        engine._analyse = analyse
        task = asyncio.create_task(engine._scan_symbol('BTCUSDT'))
        await entered.wait()
        await engine._scan_symbol('BTCUSDT')
        assert engine.metrics.counts['overlap_skipped'] == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not engine.inflight
        store.close()
    asyncio.run(run())


def test_changed_context_vetoes_slow_ai(tmp_path):
    async def run():
        store = Store(tmp_path/'engine.db')
        engine = Engine(Settings(api_key='test'), None, store)
        original = demo_snapshot('BTCUSDT'); changed = deepcopy(original)
        changed['frames']['15m']['close_time'] += 900000
        snapshots = iter([original, changed])
        async def snapshot(symbol): return next(snapshots)
        async def evaluate(state): return answer()
        engine.market.snapshot = snapshot; engine.jev.evaluate = evaluate
        await engine._scan_symbol('BTCUSDT')
        assert engine.rows['BTCUSDT']['decision'] == 'WAIT'
        assert engine.rows['BTCUSDT']['ai'] is None
        assert engine.metrics.counts['context_changed'] == 1
        store.close()
    asyncio.run(run())


def test_open_positions_survive_watchlist_cap_and_recent_expires(tmp_path):
    store = Store(tmp_path/'engine.db'); engine = Engine(Settings(priority_size=1), None, store)
    connected(engine.execution)
    engine.execution.snapshot['positions'] = [{'pair':'BTC/USDT:USDT'}, {'pair':'ETH/USDT:USDT'}]
    engine.top_volume = ('SOLUSDT','XRPUSDT')
    assert engine.priority_symbols() == ('BTCUSDT','ETHUSDT','SOLUSDT')
    value = row(); value['symbol'] = 'DOGEUSDT'; value['ai'] = answer()
    engine.rows['DOGEUSDT'] = value
    assert engine.priority_symbols()[-1] == 'DOGEUSDT'
    value['observed_at'] = 0
    assert engine.priority_symbols()[-1] == 'SOLUSDT'
    store.close()


@pytest.mark.parametrize('profile,seconds,radar,priority,ttl', [('conservative',30,2,1,180),('balanced',15,4,2,120),('aggressive',10,6,3,90)])
def test_profile_coherence(profile, seconds, radar, priority, ttl):
    s = Settings(performance_profile=profile)
    assert (s.priority_seconds,s.radar_parallelism,s.priority_parallelism,s.signal_ttl) == (seconds,radar,priority,ttl)
    assert s.priority_seconds+s.symbol_timeout < s.signal_ttl


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), True, None, -1.])
def test_pure_decision_rejects_invalid_confidence(bad):
    from app.strategy import decide
    ai=answer();ai['answers']['risk']['confidence']=bad
    assert decide(dict(candidate='LONG',guards=[]),ai,Settings())[0]=='WAIT'


def test_bulk_marks_forwarded_and_missing_mark_clears_all(tmp_path):
    async def run():
        store=Store(tmp_path/'marks.db');engine=Engine(Settings(),None,store)
        connected(engine.execution)
        engine.execution.snapshot['positions']=[{'pair':'BTC/USDT:USDT'}]
        now=int(time.time()*1000)
        premiums={'BTCUSDT':{'markPrice':'100', 'time':now}}
        async def quotes(): return time.monotonic(), 0, {}, premiums
        engine.market._quotes=quotes
        await engine.refresh_marks()
        assert engine.signals()['marks']['BTC/USDT:USDT']['price']==100
        premiums['BTCUSDT']['time']=now-16000
        await engine.refresh_marks()
        assert not engine.signals()['marks']
        store.close()
    asyncio.run(run())


def test_symbol_timeout_never_publishes_previous_entry(tmp_path):
    async def run():
        store=Store(tmp_path/'timeout.db');engine=Engine(Settings(),None,store)
        engine.rows['BTCUSDT']=row()
        async def timeout(symbol): raise TimeoutError()
        engine._analyse=timeout
        await engine._scan_symbol('BTCUSDT')
        assert engine.rows['BTCUSDT']['decision']=='WAIT'
        assert engine.metrics.counts['timeout']==1
        store.close()
    asyncio.run(run())


def test_priority_reserves_volume_slots(tmp_path):
    store=Store(tmp_path/'priority.db');engine=Engine(Settings(priority_size=2),None,store)
    engine.top_volume=('SOLUSDT',)
    for symbol in ('BTCUSDT','ETHUSDT'):
        engine.rows[symbol]={**row(), 'symbol':symbol, 'ai':answer()}
    chosen=engine.priority_symbols()
    assert 'SOLUSDT' in chosen and len(chosen)==2
    store.close()
