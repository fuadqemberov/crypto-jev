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
from app.market import demo_snapshot


def row(action='LONG'):
    return dict(symbol='BTCUSDT', observed_at=int(time.time()*1000), decision=action,
                frames={'15m': {'close_time': 12345}},
                levels={'entry': 100, 'stop': 98, 'target': 104, 'funding_cost': 0.},
                strategy='trend-continuation-v2', candidate=action, error=None)


def connected(execution):
    execution.snapshot = dict(connected=True, dry_run=True, state='running',
                             observed_at=int(time.time()*1000), positions=[])
    execution.receive_heartbeat(dict(version=4, mode='dry_run', risk_policy=execution.settings.risk.identity,
        generated_at=int(time.time()*1000), risk_ready=True, daily_loss_hit=False, equity=2000.,
        realized_today=0., unrealized=0., cooldowns={}, rejections={}, bridge_lag_ms=0.))


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
        assert c.post('/api/execution/pause', headers={'X-Crypto-Radar': '1'}).json()['paused']
        assert app.state.engine.store.paused()
    with TestClient(create_app(s, start_worker=False)) as c:
        c.auth = ('user', 'pass')
        assert c.get('/api/status').json()['execution']['paused']
        assert not c.post('/api/execution/resume', headers={'X-Crypto-Radar': '1'}).json()['paused']


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
    for key, value in [('observed_at', 0), ('observed_at', int(time.time()*1000)+10000), ('error', 'bad'), ('strategy', 'old')]:
        invalid = {**first, key: value}
        assert execution.signals([invalid])['signals'] == []
    # A pair without an entry or exit is not sent to the executor; its reason stays for the dashboard.
    payload, why_not = execution.evaluate([first], storage_error=True)
    assert payload['signals'] == [] and 'storage_error' in why_not['BTC/USDT:USDT']
    execution.snapshot['observed_at'] = 0
    assert not execution.signals([first])['entries_enabled']
    demo = Execution(Settings(demo=True), None, store); connected(demo)
    assert demo.signals([first])['signals'] == [] and 'demo' in demo.evaluate([first])[1]['BTC/USDT:USDT']
    store.close()


def test_pause_vetoes_entries_not_position_bound_exits(tmp_path):
    store = Store(tmp_path/'exit.db'); store.set_paused(True)
    execution = Execution(Settings(), None, store); connected(execution)
    value = row(); value['position_id'] = 7
    value['exit_action'] = 'CLOSE'
    signal = execution.signals([value])['signals'][0]
    assert signal['action'] == 'WAIT' and signal['close_trade_id'] == 7
    value['exit_action'] = 'HOLD'
    assert execution.signals([value])['signals'] == []
    store.close()


def test_executor_pairs_only_fresh_entries_and_open_positions(tmp_path):
    store = Store(tmp_path/'pairs.db')
    execution = Execution(Settings(), None, store)
    connected(execution)
    execution.snapshot['positions'] = [{'pair': 'ETH/USDT:USDT'}]
    stale = {**row(), 'symbol': 'SOLUSDT', 'observed_at': 0}
    waiting = {**row('WAIT'), 'symbol': 'BNBUSDT'}
    payload = execution.signals([row(), stale, waiting])
    assert payload['pairs'] == ['BTC/USDT:USDT', 'ETH/USDT:USDT']
    assert payload['refresh_period'] == 1
    store.set_paused(True)
    assert execution.signals([row()])['pairs'] == ['ETH/USDT:USDT']
    store.close()


def test_telemetry_read_only_sanitized_and_fail_closed(tmp_path):
    async def run():
        paths=[]
        payloads = {
            'show_config': {'dry_run': True, 'state': 'running', 'strategy': 'SignalBridgeStrategy', 'secret': 'never-public'},
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
    (tmp_path/'.env').write_text('CUSTOM_SETTING=keep-me\nSCAN_SECONDS=180\n', encoding='utf-8')
    path = initialize(tmp_path)
    import json
    config = json.loads(path.read_text())
    assert config['dry_run'] is True and config['dry_run_wallet'] == 2000
    assert config['exchange']['key'] == '' and config['force_entry_enable'] is False
    assert len(config['signal_bridge']['token']) >= 32
    assert 'keep-me' not in path.read_text()
    assert 'CUSTOM_SETTING=keep-me' in (tmp_path/'.env').read_text()
    assert 'SCAN_SECONDS=180' in (tmp_path/'.env').read_text()
    before = path.read_text()
    with pytest.raises(FileExistsError): initialize(tmp_path)
    assert before == path.read_text()


@pytest.mark.parametrize('values', [dict(bridge_token='short'), dict(signal_ttl=301),
    dict(freqtrade_url='https://example.com'), dict(freqtrade_url='http://user:pass@localhost'),
    dict(freqtrade_password='alone')])
def test_bridge_config_rejects_invalid(values):
    with pytest.raises(ValueError): Settings(**values)




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
            assert 1 < peak <= 6 and len(engine.rows)==3
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
    assert new['pairlists'][0]['method'] == 'RemotePairList'
    assert new['pairlists'][0]['bearer_token'] == new['signal_bridge']['token']
    assert new['exchange']==old['exchange'] and new['api_server']==old['api_server']
    assert new['signal_bridge']==old['signal_bridge'] and database.read_bytes()==b'unchanged-db'
    backups=list(directory.glob('*.backup-*'));assert len(backups)==1
    assert json.loads(backups[0].read_text())==old
    upgrade(tmp_path);assert len(list(directory.glob('*.backup-*')))==1
    new['dry_run']=False;target.write_text(json.dumps(new))
    with pytest.raises(ValueError): upgrade(tmp_path)


def test_sizing_from_risk_and_invalid_plan_does_not_block_exit(tmp_path):
    store=Store(tmp_path/'lev.db');execution=Execution(Settings(),None,store);connected(execution)
    value=row();value['position_id']=42
    value['exit_action']='CLOSE'
    output=execution.signals([value]);assert output['version']==4
    signal=output['signals'][0]
    assert signal['action']=='LONG' and signal['close_trade_id']==42
    assert signal['leverage_requested']==4
    assert execution.signals([value])['signals'][0]['leverage_requested']==4
    for levels in ({'entry':100, 'stop':102, 'target':104, 'funding_cost':0.}, None):
        value['levels']=levels
        payload,why_not=execution.evaluate([value]);signal=payload['signals'][0]
        assert signal['action']=='WAIT' and signal['close_trade_id']==42
        assert why_not['BTC/USDT:USDT']==['levels']
    store.close()
