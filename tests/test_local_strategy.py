"""Deterministic rule, freshness, migration and execution regressions; no network."""
import asyncio
from copy import deepcopy
import json
import time
import pytest
from app.config import Settings
from app.strategy import evaluate, STRATEGY_ID
from app.engine import Engine
from app.store import Store
from app.paper import build_config, upgrade
from app.bridge import VERSION, validate_payload


def snapshot(sign=1):
    now=int(time.time()*1000)
    frame=dict(open=100.-.3*sign, high=100.6, low=99.4,
               previous_high=101., previous_low=99., close=100., previous_close=100.-sign, previous_ema20=100.,
               ema20=100.-.5*sign, ema50=100.-2*sign, ema200=100.-3*sign,
               ema50_previous=100.-2.1*sign,
               atr=2., atr_pct=2., rsi=55. if sign==1 else 45.,
               macd_hist=.1*sign, macd_change=.01*sign, relative_volume=1.2, close_time=now-1000)
    return dict(symbol='BTCUSDT', observed_at=now, bid=99.995, ask=100.005, mark=100.,
                spread_bps=1., funding_rate=0., frames={tf:deepcopy(frame) for tf in ('15m','1h','4h')})


@pytest.mark.parametrize('sign,direction',[(1,'LONG'),(-1,'SHORT')])
def test_symmetric_entries_and_fixed_geometry(sign,direction):
    value=evaluate(snapshot(sign),Settings())
    assert value['decision']==direction and value['strategy']==STRATEGY_ID
    assert not value['rejection_codes'] and all(g['passed'] for g in value['guards'])
    levels=value['levels']
    assert sign*(levels['entry']-levels['stop'])==pytest.approx(4.)
    assert sign*(levels['target']-levels['entry'])==pytest.approx(8.)
    assert levels['net_rr']<2


@pytest.mark.parametrize('field,bad,code',[
    ('macd_hist',-.1,'momentum'),
    ('rsi',75.,'rsi'),('relative_volume',.6,'volume'),
    ('atr_pct',6.,'volatility'),('ema20',97.,'extension')])
def test_each_entry_gate_is_effective(field,bad,code):
    snap=snapshot();snap['frames']['15m'][field]=bad
    result=evaluate(snap,Settings())
    assert result['decision']=='WAIT' and code in result['rejection_codes']
    assert result['levels'] is None


@pytest.mark.parametrize('sign',[1,-1])
def test_no_signal_without_higher_timeframe_alignment(sign):
    snap=snapshot(sign);snap['frames']['4h']['ema50_previous']=snap['frames']['4h']['ema50']
    assert 'trend' in evaluate(snap,Settings())['rejection_codes']


def test_ema_pullback_does_not_need_exact_single_candle_reclaim():
    snap=snapshot(1)
    snap['frames']['15m']['previous_close']=101.
    result=evaluate(snap,Settings())
    assert result['decision']=='LONG' and result['setup']=='pullback'


def test_breakout_needs_stronger_volume_and_momentum():
    snap=snapshot(1);frame=snap['frames']['15m']
    frame.update(open=100.2,low=99.9,atr=1.5,previous_close=101.,
                 previous_high=99.9,close=100.,relative_volume=.8)
    result=evaluate(snap,Settings())
    assert result['decision']=='WAIT' and 'volume' in result['rejection_codes']
    frame['relative_volume']=1.2;frame['macd_change']=-.01
    assert 'momentum' in evaluate(snap,Settings())['rejection_codes']


@pytest.mark.parametrize('bad',[None,True,float('nan'),float('inf'),'100'])
@pytest.mark.parametrize('field',['atr','close','previous_close','relative_volume','rsi','close_time'])
def test_malformed_features_fail_closed(bad,field):
    snap=snapshot();snap['frames']['15m'][field]=bad
    result=evaluate(snap,Settings(),{'is_short':False})
    assert result['decision']=='WAIT' and result['exit_action']=='HOLD'
    assert result['rejection_codes']==['market_data']


@pytest.mark.parametrize('mutation',['missing','stale_quote','future_quote','stale_candle','future_candle','crossed_book'])
def test_missing_or_stale_inputs_do_not_create_decisions(mutation):
    snap=snapshot();now=snap['observed_at']
    if mutation=='missing':del snap['frames']['1h']
    if mutation=='stale_quote':snap['observed_at']=now-15001
    if mutation=='future_quote':snap['observed_at']=now+1
    if mutation=='stale_candle':snap['frames']['4h']['close_time']=now-14430001
    if mutation=='future_candle':snap['frames']['15m']['close_time']=now
    if mutation=='crossed_book':snap['bid']=snap['ask']+1
    assert evaluate(snap,Settings(),now=now)['rejection_codes']==['market_data']


@pytest.mark.parametrize('short',[True,False])
def test_thesis_exit_requires_two_closed_timeframes(short):
    snap=snapshot(-1 if short else 1);held=-1 if short else 1
    snap['frames']['1h']['close']=snap['frames']['1h']['ema50']-held
    snap['frames']['1h'].update(open=snap['frames']['1h']['close'],
        high=snap['frames']['1h']['close']+.1,low=snap['frames']['1h']['close']-.1)
    assert evaluate(snap,Settings(),{'is_short':short})['exit_action']=='HOLD'
    snap['frames']['15m']['macd_hist']=-held*.1
    result=evaluate(snap,Settings(),{'is_short':short})
    assert result['exit_action']=='CLOSE'
    assert result['decision']=='WAIT'
    snap['observed_at']=0
    assert evaluate(snap,Settings(),{'is_short':short})['exit_action']=='HOLD'


def test_engine_runs_without_external_decision_api(tmp_path):
    async def run():
        class NoNetwork:
            async def get(self,*args,**kwargs):raise AssertionError('Unexpected HTTP')
            async def post(self,*args,**kwargs):raise AssertionError('Unexpected HTTP')
        store=Store(tmp_path/'history.db')
        engine=Engine(Settings(symbols=('BTCUSDT',)),NoNetwork(),store)
        async def market(symbol):return snapshot()
        engine.market.snapshot=market
        await engine.scan()
        assert engine.rows['BTCUSDT']['decision']=='LONG'
        assert store.history()[0]['strategy']==STRATEGY_ID
        assert not engine.signals()['entries_enabled'] # No execution telemetry.
        store.close()
    asyncio.run(run())


def test_upgrade_refuses_unknown_strategy_config(tmp_path):
    config=build_config(Settings(),'x'*32,'test','secret')
    config['strategy']='JevBridgeStrategy'
    root=tmp_path/'user_data';root.mkdir();target=root/'config.paper.json'
    target.write_text(json.dumps(config))
    with pytest.raises(ValueError):upgrade(tmp_path)
    assert json.loads(target.read_text())==config


def test_old_bridge_protocol_is_rejected():
    payload=dict(version=3,mode='dry_run',risk_policy=Settings().risk.identity,
                 generated_at=int(time.time()*1000),entries_enabled=True,signals=[])
    assert VERSION==4
    with pytest.raises(ValueError):validate_payload(payload,payload['generated_at'],Settings().risk.identity)


@pytest.mark.parametrize('values',[{'stop_atr':0},{'target_r':float('nan')},{'min_relative_volume':True},
                                  {'min_atr_pct':1,'max_atr_pct':.5},{'rsi_long_max':90}])
def test_rule_configuration_rejects_invalid_values(values):
    from app.config import EntryRules
    with pytest.raises(ValueError):EntryRules(**values)


def test_rule_environment_is_applied(monkeypatch):
    monkeypatch.setenv('RULE_MIN_RELATIVE_VOLUME','1.5')
    result=evaluate(snapshot(),Settings.load())
    assert result['decision']=='WAIT' and 'volume' in result['rejection_codes']


def test_future_open_candle_does_not_affect_features():
    from app.indicators import closed_bars,features
    from test_analysis import raw_bars
    raw=raw_bars();now=raw[-1][6]+1
    expected=features(closed_bars(raw,'15m',now))
    # An extreme future/open candle must not leak into any signal input.
    future=[now,10000,20000,1,19000,999999,now+900000-1]
    assert features(closed_bars(raw+[future],'15m',now))==expected


@pytest.mark.parametrize('mutation',[{'observed_at':None},{'frames':{}},{'frames':None},{'symbol':None},{'decision':'bad'}])
def test_bridge_rejects_malformed_local_rows_without_crashing(tmp_path,mutation):
    from app.execution import Execution
    from test_execution import row,connected
    store=Store(tmp_path/'history.db');execution=Execution(Settings(),None,store);connected(execution)
    result=execution.signals([{**row(),**mutation}])
    assert result['signals']==[] and result['diagnostics']['rejections']['schema']==1
    store.close()
