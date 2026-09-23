import asyncio
import time
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.execution import Execution, pair_for
from app.main import create_app
from app.paper import initialize, build_config, upgrade
from app.store import Store
from app.engine import Engine
from app.jev import Jev, JevError
from app.market import demo_snapshot
from test_analysis import answer


def row(action='LONG'):
    return dict(symbol='BTCUSDT', observed_at=int(time.time()*1000), decision=action,
                frames={'15m': {'close_time': 12345}},
                levels={'entry': 100, 'stop': 98, 'target': 104},
                ai={'answers': {'leverage': {'choice': '5', 'confidence': .95}}}, error=None, ai_error=None)


def connected(execution):
    execution.snapshot = dict(connected=True, dry_run=True, state='running',
                             observed_at=int(time.time()*1000), positions=[])


def test_bridge_auth_pause_and_restart(tmp_path):
    s = Settings(data_dir=tmp_path, bridge_token='x'*32, user='user', password='pass')
    app = create_app(s, start_worker=False)
    with TestClient(app) as c:
        assert c.get('/api/execution/signals').status_code == 401
        response = c.get('/api/execution/signals', headers={'Authorization': 'Bearer '+'x'*32})
        assert response.status_code == 200 and response.json()['entries_enabled'] is False
        assert c.get('/api/status', headers={'Authorization': 'Bearer '+'x'*32}).status_code == 401
        c.auth = ('user', 'pass')
        assert c.post('/api/execution/pause').status_code == 403
        assert c.post('/api/execution/pause', headers={'X-Crypto-Jev': '1'}).json()['paused']
        assert app.state.engine.store.paused()
    with TestClient(create_app(s, start_worker=False)) as c:
        c.auth = ('user', 'pass')
        assert c.get('/api/status').json()['execution']['paused']
        assert not c.post('/api/execution/resume', headers={'X-Crypto-Jev': '1'}).json()['paused']


def test_signals_stale_demo_errors_and_idempotent_identity(tmp_path):
    store = Store(tmp_path/'bridge.db')
    execution = Execution(Settings(bridge_token='x'*32), None, store)
    connected(execution)
    first = row()
    signal = execution.signals([first])['signals'][0]
    assert signal['action'] == 'LONG' and signal['pair'] == 'BTC/USDT:USDT'
    second = deepcopy(first); second['observed_at'] += 1
    # Fresh timestamps cannot cause repeat entry within the same candle.
    time.sleep(.003)
    assert execution.signals([second])['signals'][0]['id'] == signal['id']
    for key, value in [('observed_at', 0), ('observed_at', int(time.time()*1000)+10000), ('error', 'bad'), ('ai_error', 'bad')]:
        invalid = {**first, key: value}
        assert execution.signals([invalid])['signals'] == []
    assert execution.signals([first], storage_error=True)['signals'][0]['action'] == 'WAIT'
    execution.snapshot['observed_at'] = 0
    assert not execution.signals([first])['entries_enabled']
    demo = Execution(Settings(demo=True), None, store); connected(demo)
    assert demo.signals([first])['signals'][0]['action'] == 'WAIT'
    store.close()


def test_pause_vetoes_entries_not_position_bound_exits(tmp_path):
    store = Store(tmp_path/'exit.db'); store.set_paused(True)
    execution = Execution(Settings(), None, store); connected(execution)
    value = row(); value['position_id'] = 7
    value['ai']['answers']['position_action'] = {'choice': 'CLOSE', 'confidence': .95}
    signal = execution.signals([value])['signals'][0]
    assert signal['action'] == 'WAIT' and signal['close_trade_id'] == 7
    value['ai']['answers']['position_action']['confidence'] = .5
    assert 'close_trade_id' not in execution.signals([value])['signals'][0]
    store.close()


def test_telemetry_read_only_sanitized_and_fail_closed(tmp_path):
    async def run():
        paths=[]
        payloads = {
            'show_config': {'dry_run': True, 'state': 'running', 'strategy': 'JevBridgeStrategy', 'secret': 'never-public'},
            'status': [{'trade_id': 1, 'pair': 'BTC/USDT:USDT', 'is_short': False, 'secret': 'never-public'}],
            'profit': {'profit_closed_coin': 2, 'profit_all_coin': 3},
            'balance': {'currencies': [{'currency': 'USDT', 'balance': 2002, 'free': 1862, 'used': 140}]},
            'trades': {'trades': []}}
        def handler(request):
            assert request.method == 'GET'
            paths.append(request.url.path)
            return httpx.Response(200, json=payloads[request.url.path.split('/')[-1]])
        store=Store(tmp_path/'read.db')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            execution=Execution(Settings(freqtrade_user='test', freqtrade_password='secret'), client, store)
            await execution.refresh()
            assert execution.status()['connected'] and execution.status()['wallet'] == 2002
            assert 'never-public' not in str(execution.status())
            assert execution.position('BTCUSDT')['trade_id'] == 1
            payloads['show_config']['dry_run'] = False
            await execution.refresh()
            assert not execution.status()['connected'] and execution.position('BTCUSDT') is None
            assert 'wallet' not in execution.status()
        store.close()
    asyncio.run(run())


def test_initializer_preserves_key_and_refuses_overwrite(tmp_path):
    (tmp_path/'.env').write_text('TYPESAFE_API_KEY=keep-me\nSCAN_SECONDS=180\n', encoding='utf-8')
    path = initialize(tmp_path)
    import json
    config = json.loads(path.read_text())
    assert config['dry_run'] is True and config['dry_run_wallet'] == 2000
    assert config['exchange']['key'] == '' and config['force_entry_enable'] is False
    assert len(config['jev_bridge']['token']) >= 32
    assert 'keep-me' not in path.read_text()
    assert 'TYPESAFE_API_KEY=keep-me' in (tmp_path/'.env').read_text()
    assert 'SCAN_SECONDS=180' in (tmp_path/'.env').read_text()
    before = path.read_text()
    with pytest.raises(FileExistsError): initialize(tmp_path)
    assert before == path.read_text()


@pytest.mark.parametrize('values', [dict(bridge_token='short'), dict(signal_ttl=301),
    dict(freqtrade_url='https://example.com'), dict(freqtrade_url='http://user:pass@localhost'),
    dict(freqtrade_password='alone')])
def test_bridge_config_rejects_invalid(values):
    with pytest.raises(ValueError): Settings(**values)


def test_position_jev_contract_and_missing_answer_rejected():
    async def run():
        response=answer()
        response['answers']['position_action']={'type':'choice','choice':'CLOSE','confidence':.95,
                                               'probabilities':{'CLOSE':1.,'HOLD':0.}}
        def handler(request):
            import json
            payload=json.loads(request.content)
            assert 'position_action' in payload['questions']
            return httpx.Response(200,json=response)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            jev=Jev(client,Settings(api_key='test'))
            result=await jev.evaluate({'position':{'trade_id':7}})
            assert result['answers']['position_action']['choice']=='CLOSE'
            del response['answers']['position_action']
            with pytest.raises(JevError): await jev.evaluate({'position':{'trade_id':7}})
    asyncio.run(run())


def test_bounded_parallel_scan_and_disk_failure_veto(tmp_path):
    async def run():
        active=0; peak=0
        store=Store(tmp_path/'parallel.db')
        async with httpx.AsyncClient() as client:
            engine=Engine(Settings(symbols=('BTCUSDT','ETHUSDT','SOLUSDT')),client,store)
            async def snapshot(symbol):
                nonlocal active,peak
                active+=1;peak=max(peak,active)
                await asyncio.sleep(.01)
                active-=1
                return demo_snapshot(symbol)
            engine.market.snapshot=snapshot
            await engine.scan()
            assert peak==2 and len(engine.rows)==3
            def fail(value): raise OSError('disk full')
            store.append=fail
            await engine.scan()
            assert engine.storage_error
            assert all(r['decision']=='WAIT' and r['error'] for r in engine.rows.values())
        store.close()
    asyncio.run(run())


def test_upgrade_preserves_local_credentials_symbols_and_database(tmp_path):
    import json
    directory=tmp_path/'user_data';directory.mkdir()
    target=directory/'config.paper.json'
    old=build_config(Settings(), 'secret-token','my-user','my-password')
    old.update(max_open_trades=5,stake_amount='unlimited')
    old['exchange']['pair_whitelist']=['DOGE/USDT:USDT']
    target.write_text(json.dumps(old))
    database=tmp_path/'wallet.sqlite';database.write_bytes(b'unchanged-db')
    upgrade(tmp_path)
    new=json.loads(target.read_text())
    assert new['max_open_trades']==-1 and new['stake_amount']==140 and new['stoploss']==-.5
    assert new['exchange']==old['exchange'] and new['api_server']==old['api_server']
    assert new['jev_bridge']==old['jev_bridge'] and database.read_bytes()==b'unchanged-db'
    backups=list(directory.glob('*.backup-*'));assert len(backups)==1
    assert json.loads(backups[0].read_text())==old
    upgrade(tmp_path);assert len(list(directory.glob('*.backup-*')))==1
    new['dry_run']=False;target.write_text(json.dumps(new))
    with pytest.raises(ValueError): upgrade(tmp_path)


def test_missing_or_uncertain_leverage_blocks_entry_but_not_exit(tmp_path):
    store=Store(tmp_path/'lev.db');execution=Execution(Settings(),None,store);connected(execution)
    value=row();value['position_id']=42
    value['ai']['answers']={'position_action':{'choice':'CLOSE','confidence':.99}}
    output=execution.signals([value]);assert output['version']==2
    assert output['signals'][0]['action']=='WAIT' and output['signals'][0]['close_trade_id']==42
    value['ai']['answers']['leverage']={'choice':'100','confidence':.5}
    assert execution.signals([value])['signals'][0]['action']=='WAIT'
    value['ai']['answers']['leverage']['confidence']=.99
    assert execution.signals([value])['signals'][0]['leverage_requested']==100
    store.close()
