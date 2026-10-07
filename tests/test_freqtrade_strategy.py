"""Tests use the real installed Freqtrade API; install .[paper,test] to run."""
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip('freqtrade')
from freqtrade.enums import RunMode
from freqtrade.configuration.config_validation import validate_config_consistency
from freqtrade.resolvers import StrategyResolver
from freqtrade.persistence import Trade, init_db
from app.config import Settings
from app.paper import build_config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def strategy():
    init_db('sqlite://')
    config = build_config(Settings(), 'x'*32, 'test', 'test-password')
    config.update(runmode=RunMode.DRY_RUN, user_data_dir=ROOT/'user_data',
                  strategy_path=str(ROOT/'user_data/strategies'))
    instance = StrategyResolver.load_strategy(config)
    validate_config_consistency(config)
    instance.bot_start()
    instance.wallets = SimpleNamespace(get_total_stake_amount=lambda: 2000, get_available_stake_amount=lambda: 2000)
    instance.dp = SimpleNamespace(orderbook=lambda pair, depth: {'bids': [[99.995, 1]], 'asks': [[100.005, 1]]})
    yield instance
    instance.executor.shutdown(wait=True, cancel_futures=True)
    Trade.session.remove()


def signal(action='LONG'):
    now = datetime.now(timezone.utc)
    return dict(id='a'*24, pair='BTC/USDT:USDT', action=action, leverage_requested=1, funding_cost=0.,
                observed_at=int(now.timestamp()*1000)-100, expires_at=int(now.timestamp()*1000)+120000,
                levels=dict(entry=100, stop=98 if action=='LONG' else 102, target=104 if action=='LONG' else 96))


def test_dynamic_whitelist_uses_resolved_markets(strategy):
    strategy.config['exchange']['pair_whitelist'] = ['.*/USDT:USDT']
    strategy.dp.current_whitelist = lambda: ['BTC/USDT:USDT']
    value = signal()
    load(strategy, value)
    assert value['pair'] in strategy.signals
    strategy.dp.current_whitelist = lambda: ['ETH/USDT:USDT']
    load(strategy, value)
    assert not strategy.signals


def test_bridge_to_entry_and_risk_sizing(strategy, tmp_path, monkeypatch):
    import time
    import pandas as pd
    from app.execution import Execution
    from app.store import Store
    store = Store(tmp_path/'flow.db')
    execution = Execution(Settings(), None, store)
    from test_execution import connected
    connected(execution)
    value = signal()
    row = dict(symbol='BTCUSDT', observed_at=value['observed_at'], decision='LONG',
               levels={**value['levels'], 'funding_cost': 0.}, frames={'15m': {'close_time': 123}},
               strategy='trend-continuation-v2',
               error=None)
    payload = execution.signals([row])
    strategy.dp.current_whitelist = lambda: payload['pairs']
    now = datetime.now(timezone.utc)
    strategy._accept(payload, now.timestamp()*1000)
    data = strategy.populate_entry_trend(pd.DataFrame({'close':[100,100], 'volume':[1,1]}), {'pair':value['pair']})
    assert data.iloc[-1].enter_long == 1
    assert payload['signals'][0]['leverage_requested'] == 4
    entry_tag = data.iloc[-1].enter_tag
    assert strategy.leverage(value['pair'],now,100,1,20,entry_tag,'long') == 4
    monkeypatch.setattr(Trade, 'get_trades_proxy', lambda **kw: [])
    strategy.wallets = SimpleNamespace(get_total_stake_amount=lambda: 2000, get_available_stake_amount=lambda: 2000)
    assert strategy.custom_stake_amount(value['pair'],now,100,400,5,2000,1,entry_tag,'long') == 140
    assert strategy.confirm_trade_entry(value['pair'],'market',1.4,100,'GTC',now,entry_tag,'long')
    store.close()


def load(strategy, value, enabled=True):
    now=datetime.now(timezone.utc)
    strategy._accept(dict(version=4, risk_policy=strategy.policy.identity, mode='dry_run', generated_at=int(now.timestamp()*1000),
                          entries_enabled=enabled, signals=[value]), now.timestamp()*1000)
    return now


def tag(value):
    return f"rule:{value['id']}:{value['levels']['stop']}:{value['levels']['target']}:{value['leverage_requested']}"


def test_real_framework_loads_config_and_dry_only(strategy):
    assert strategy.can_short and strategy.timeframe == '1m'
    strategy.config['dry_run'] = False
    with pytest.raises(ValueError): strategy.bot_start()
    strategy.config['dry_run'] = True
    strategy.config['runmode'] = RunMode.BACKTEST
    with pytest.raises(ValueError): strategy.bot_start()


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_entry_confirmation_and_7_percent_margin(strategy, monkeypatch, side):
    value=signal(side); now=load(strategy,value)
    monkeypatch.setattr(Trade, 'get_trades_proxy', lambda **kw: [])
    strategy.wallets=SimpleNamespace(get_total_stake_amount=lambda: 2000, get_available_stake_amount=lambda: 2000)
    assert strategy.confirm_trade_entry(value['pair'],'market',1,100,'GTC',now,tag(value),side.lower())
    assert not strategy.confirm_trade_entry(value['pair'],'market',1,101,'GTC',now,tag(value),side.lower())
    assert strategy.custom_stake_amount(value['pair'],now,100,400,5,2000,1,tag(value),side.lower()) == 140
    assert strategy.custom_stake_amount(value['pair'],now,100,400,200,2000,1,tag(value),side.lower()) == 0
    strategy.wallets.get_total_stake_amount=Mock(side_effect=RuntimeError('offline'))
    assert strategy.custom_stake_amount(value['pair'],now,100,400,5,2000,1,tag(value),side.lower()) == 0


def test_daily_loss_and_duplicate_persisted_trade(strategy, monkeypatch):
    value=signal(); now=load(strategy,value)
    monkeypatch.setattr(Trade, 'get_trades_proxy', lambda **kw: [SimpleNamespace(close_profit_abs=-60, enter_tag='')])
    assert not strategy.confirm_trade_entry(value['pair'],'market',1,100,'GTC',now,tag(value),'long')
    init_db('sqlite://')
    existing=Trade(pair=value['pair'], exchange='binance', enter_tag=tag(value),
                   open_rate=100, stake_amount=140, amount=1.4, is_open=False,
                   open_date=now-timedelta(days=1), close_date=now-timedelta(days=1),
                   close_profit_abs=1, fee_open=.0005, fee_close=.0005)
    Trade.session.add(existing); Trade.commit()
    monkeypatch.undo()
    assert not strategy.confirm_trade_entry(value['pair'],'market',1,100,'GTC',now,tag(value),'long')
    Trade.session.remove()


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_stop_target_and_thesis_exit_are_independent_of_entry_permission(strategy, side):
    value=signal(side); value['close_trade_id']=7
    now=load(strategy,value,enabled=False)
    trade=SimpleNamespace(id=7, enter_tag=tag(value), is_short=side=='SHORT', leverage=1, open_date_utc=now)
    assert strategy.custom_stoploss(value['pair'],trade,now,100,0) == pytest.approx(.02)
    assert strategy.custom_exit(value['pair'],trade,now,100,0) == 'thesis_exit'
    trade.id=8
    assert strategy.custom_exit(value['pair'],trade,now,100,0) is None
    strategy.signals={}
    assert strategy.custom_exit(value['pair'],trade,now,value['levels']['target'],.04)=='risk_target'
    assert strategy.custom_exit(value['pair'],trade,now,value['levels']['stop'],-.02)=='risk_stop'
    assert strategy.custom_exit(value['pair'],trade,now+timedelta(hours=4),100,0)=='risk_max_hold'


def test_stale_nan_and_invalid_levels_fail_closed(strategy):
    for mutation in [dict(expires_at=0),dict(observed_at=float('nan')),dict(levels={'entry':100,'stop':105,'target':104})]:
        value={**signal(),**mutation};load(strategy,value)
        assert strategy.signals=={}


def test_current_spread_and_slippage_rejected(strategy,monkeypatch):
    value=signal();now=load(strategy,value)
    monkeypatch.setattr(Trade,'get_trades_proxy',lambda **kw: [])
    strategy.dp.orderbook=lambda pair,depth: {'bids':[[99,1]],'asks':[[101,1]]}
    assert not strategy.confirm_trade_entry(value['pair'],'market',1,100,'GTC',now,tag(value),'long')


def test_vector_entry_columns_and_expiry(strategy):
    import pandas as pd
    value=signal('SHORT');load(strategy,value)
    data=pd.DataFrame({'close':[100,100],'volume':[1,1]})
    result=strategy.populate_entry_trend(data,{'pair':value['pair']})
    assert result.enter_short.tolist()==[0,1] and result.enter_long.tolist()==[0,0]
    strategy.signals[value['pair']]['expires_at']=0
    result=strategy.populate_entry_trend(data,{'pair':value['pair']})
    assert result.enter_short.tolist()==[0,0]


def test_background_failure_clears_previous_signal(strategy):
    load(strategy,signal())
    failed=Future();failed.set_exception(RuntimeError('offline'))
    strategy.pending=failed
    strategy.executor.shutdown()
    strategy.executor=Mock()
    strategy.bot_loop_start(datetime.now(timezone.utc))
    assert strategy.signals=={} and not strategy.entries_enabled


# Risk-based target: 2% stop -> 4x, 0.2% stop -> 20x; requested leverage, exchange max and the 20x ceiling cap it.
@pytest.mark.parametrize('requested,stop,exchange_max,expected', [(5,98,125,4),(100,98,125,4),(2,98,125,2),(100,99.8,125,20),(100,99.8,10,10),(100,99.9,125,20)])
def test_leverage_is_dynamic_and_capped(strategy,requested,stop,exchange_max,expected):
    value=signal();value['leverage_requested']=requested;value['levels']['stop']=stop
    now=load(strategy,value)
    assert strategy.leverage(value['pair'],now,100,1,exchange_max,tag(value),'long')==expected


@pytest.mark.parametrize('bad', [None,0,-1,101,True,5.5,float('nan'),'100'])
def test_invalid_leverage_never_becomes_entry(strategy,bad):
    value=signal();value['leverage_requested']=bad;load(strategy,value)
    assert strategy.signals=={}


def test_higher_leverage_reduces_margin_with_same_risk_budget(strategy,monkeypatch):
    value=signal();value['leverage_requested']=20;now=load(strategy,value)
    monkeypatch.setattr(Trade,'get_trades_proxy',lambda **kw: [])
    strategy.wallets=SimpleNamespace(get_total_stake_amount=lambda:2000, get_available_stake_amount=lambda:2000)
    stake=strategy.custom_stake_amount(value['pair'],now,100,140,1,2000,20,tag(value),'long')
    assert stake==pytest.approx(10/(.021584*20))
    assert stake*20*.021584==pytest.approx(10)


def test_unlimited_count_uses_numeric_stake_and_preserves_old_trade_plan(strategy):
    assert strategy.config['max_open_trades']==-1
    assert isinstance(strategy.config['stake_amount'],(int,float))
    strategy.executor.shutdown();strategy.executor=Mock()
    strategy.wallets=SimpleNamespace(get_total_stake_amount=lambda:3000,get_available_stake_amount=lambda:75)
    strategy.bot_loop_start(datetime.now(timezone.utc))
    assert strategy.config['stake_amount']==75
    trade=SimpleNamespace(id=7,enter_tag='rule:old:98:104',is_short=False,leverage=1,open_date_utc=datetime.now(timezone.utc))
    assert strategy.custom_stoploss('BTC/USDT:USDT',trade,datetime.now(timezone.utc),100,0)==pytest.approx(.02)


def test_upgrade_restores_tighter_stop_after_freqtrade_reinitializes(strategy):
    now=datetime.now(timezone.utc)
    trade=Trade(pair='BTC/USDT:USDT',exchange='binance',enter_tag='rule:old:90:120',
                open_rate=100,stake_amount=140,amount=1.4,is_open=True,open_date=now,
                fee_open=.0005,fee_close=.0005,leverage=1,is_short=False,
                stop_loss=95,initial_stop_loss=95,initial_stop_loss_pct=-.05,
                is_stop_loss_trailing=False,price_precision=8,precision_mode_price=2)
    Trade.session.add(trade);Trade.commit()
    strategy.preserved_stops={trade.id:95}
    strategy.marks={'BTC/USDT:USDT': {'price':100, 'observed_at':now.timestamp()*1000}}
    Trade.stoploss_reinitialization(-.5)
    assert trade.stop_loss==50
    strategy.executor.shutdown();strategy.executor=Mock()
    strategy.wallets=SimpleNamespace(get_total_stake_amount=lambda:2000,get_available_stake_amount=lambda:1860)
    strategy.bot_loop_start(now)
    assert trade.stop_loss==95
    assert strategy.custom_stoploss(trade.pair,trade,now,100,0)==pytest.approx(.05)


def test_envelope_expiry_revokes_signal_before_its_own_ttl(strategy):
    value=signal(); now=load(strategy,value)
    assert strategy._signal(value['pair'], now)
    assert strategy._signal(value['pair'], now+timedelta(seconds=16)) is None


def test_real_exchange_credentials_rejected(strategy):
    strategy.config['exchange']['key']='not-a-real-key'
    with pytest.raises(ValueError, match='credentials'): strategy.bot_start()


def test_daily_equity_cooldown_and_stale_mark(strategy, monkeypatch):
    now=datetime.now(timezone.utc)
    closed=SimpleNamespace(pair='BTC/USDT:USDT', close_profit_abs=-70., close_date_utc=now-timedelta(seconds=10))
    monkeypatch.setattr(Trade,'get_trades_proxy',lambda **kw: [closed] if kw.get('is_open') is False else [])
    strategy.wallets.get_total_stake_amount=lambda:4000.
    risk=strategy._risk_state(now)
    assert risk['equity']==4000 and not risk['daily_loss_hit']
    assert risk['cooldowns']['BTC/USDT:USDT'] > now.timestamp()*1000
    strategy.wallets.get_total_stake_amount=lambda:2000.
    assert strategy._risk_state(now)['daily_loss_hit']
    opened=SimpleNamespace(pair='ETH/USDT:USDT',calc_profit=lambda rate: -50.)
    monkeypatch.setattr(Trade,'get_open_trades',lambda: [opened])
    strategy.marks={'ETH/USDT:USDT': {'price':100., 'observed_at':now.timestamp()*1000-16000}}
    assert not strategy._heartbeat(now)['risk_ready']
    strategy.marks={'ETH/USDT:USDT': {'price':100., 'observed_at':now.timestamp()*1000}}
    assert strategy._risk_state(now)['equity']==1950.


def test_strategy_and_engine_share_daily_veto(strategy, tmp_path, monkeypatch):
    from app.execution import Execution
    from app.store import Store
    from test_execution import connected, row
    now=datetime.now(timezone.utc)
    closed=SimpleNamespace(pair='ETH/USDT:USDT',close_profit_abs=-70.,close_date_utc=now-timedelta(hours=1))
    monkeypatch.setattr(Trade,'get_trades_proxy',lambda **kw: [closed] if kw.get('is_open') is False else [])
    store=Store(tmp_path/'shared.db'); execution=Execution(Settings(), None, store); connected(execution)
    report=strategy._heartbeat(now)
    execution.receive_heartbeat(report)
    assert 'daily_loss' in execution.signals([row()])['diagnostics']['blocks']
    strategy.entries_enabled=True
    assert not strategy._entry_guard('BTC/USDT:USDT', now)
    store.close()


def test_short_stop_restored_once_and_then_read_from_trade(strategy):
    now=datetime.now(timezone.utc)
    trade=Trade(pair='BTC/USDT:USDT',exchange='binance',enter_tag='rule:old:110:80',
        open_rate=100,stake_amount=140,amount=1.4,is_open=True,open_date=now,
        fee_open=.0005,fee_close=.0005,leverage=1,is_short=True,
        stop_loss=105,initial_stop_loss=105,initial_stop_loss_pct=-.05,
        is_stop_loss_trailing=False,price_precision=8,precision_mode_price=2)
    Trade.session.add(trade);Trade.commit()
    strategy.preserved_stops={trade.id:105}
    Trade.stoploss_reinitialization(-.5)
    strategy._restore_stops()
    assert trade.stop_loss==105 and not strategy.preserved_stops
    assert strategy.custom_stoploss(trade.pair,trade,now,100,0)==pytest.approx(.05)


def test_final_confirmation_checks_actual_amount_risk(strategy, monkeypatch):
    value=signal();now=load(strategy,value)
    monkeypatch.setattr(Trade,'get_trades_proxy',lambda **kw: [])
    assert not strategy.confirm_trade_entry(value['pair'],'market',100,100,'GTC',now,tag(value),'long')
    assert strategy.rejections['margin']==1


@pytest.mark.parametrize('short,open_rate,plan,low,high', [
    (True, .04877, .0492950097386, .0485, .0492),   # LAB 2026-10-05: stop walked to .04915
    (True, .06029, .0609884446833, .0600, .0609),   # SLX 2026-10-05: stop walked to .06059
    (False, .816, .803643201612, .805, .83),
])
def test_unchanged_plan_stop_does_not_creep_toward_price(strategy, short, open_rate, plan, low, high):
    import random
    now=datetime.now(timezone.utc)
    stop, target = (plan, open_rate*.97) if short else (plan, open_rate*1.03)
    trade=Trade(pair='LAB/USDT:USDT',exchange='binance',enter_tag=f'rule:{"b"*24}:{stop:.12g}:{target:.12g}:6',
                open_rate=open_rate,stake_amount=140,amount=1000,is_open=True,open_date=now,
                fee_open=.0005,fee_close=.0005,leverage=6,is_short=short,
                price_precision=.00001 if open_rate < .1 else .0001,precision_mode_price=4)
    Trade.session.add(trade);Trade.commit()
    strategy.ft_stoploss_adjust(open_rate, trade, now, 0, 0, after_fill=True)
    placed=trade.stop_loss
    rng=random.Random(1)
    for _ in range(2000):
        rate=round(rng.uniform(low, high)/trade.price_precision)*trade.price_precision
        strategy.ft_stoploss_adjust(rate, trade, now, 0, 0)
    assert trade.stop_loss==placed==pytest.approx(plan, rel=3e-4)
    assert not trade.is_stop_loss_trailing


@pytest.mark.parametrize('short',[False,True])
def test_unknown_trade_tag_is_closed_instead_of_guessing_a_plan(strategy,short):
    value=signal('SHORT' if short else 'LONG');now=load(strategy,value)
    trade=SimpleNamespace(id=7,enter_tag=tag(value).replace('rule:','jev:',1),is_short=short,leverage=1,open_date_utc=now)
    assert strategy.custom_stoploss(value['pair'],trade,now,100,0) is None
    assert strategy.custom_exit(value['pair'],trade,now,100,0)=='missing_risk_plan'
