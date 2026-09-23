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
    instance.dp = SimpleNamespace(orderbook=lambda pair, depth: {'bids': [[99.995, 1]], 'asks': [[100.005, 1]]})
    yield instance
    instance.executor.shutdown(wait=True, cancel_futures=True)
    Trade.session.remove()


def signal(action='LONG'):
    now = datetime.now(timezone.utc)
    return dict(id='a'*24, pair='BTC/USDT:USDT', action=action, leverage_requested=1,
                observed_at=int(now.timestamp()*1000)-100, expires_at=int(now.timestamp()*1000)+120000,
                levels=dict(entry=100, stop=98 if action=='LONG' else 102, target=104 if action=='LONG' else 96))


def load(strategy, value, enabled=True):
    now=datetime.now(timezone.utc)
    strategy._accept(dict(version=2, mode='dry_run', generated_at=int(now.timestamp()*1000),
                          entries_enabled=enabled, signals=[value]), now.timestamp()*1000)
    return now


def tag(value):
    return f"jev:{value['id']}:{value['levels']['stop']}:{value['levels']['target']}:{value['leverage_requested']}"


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
    strategy.wallets=SimpleNamespace(get_total_stake_amount=lambda: 2000)
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
def test_stop_target_and_jev_exit_are_independent_of_entry_permission(strategy, side):
    value=signal(side); value['close_trade_id']=7
    now=load(strategy,value,enabled=False)
    trade=SimpleNamespace(id=7, enter_tag=tag(value), is_short=side=='SHORT', leverage=1, open_date_utc=now)
    assert strategy.custom_stoploss(value['pair'],trade,now,100,0) == pytest.approx(.02)
    assert strategy.custom_exit(value['pair'],trade,now,100,0) == 'jev_close'
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


@pytest.mark.parametrize('requested,stop,exchange_max,expected', [(5,98,125,5),(100,98,125,23),(100,99.8,125,100),(100,99.8,20,20)])
def test_jev_leverage_is_dynamic_and_capped(strategy,requested,stop,exchange_max,expected):
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
    strategy.wallets=SimpleNamespace(get_total_stake_amount=lambda:2000)
    stake=strategy.custom_stake_amount(value['pair'],now,100,140,1,2000,20,tag(value),'long')
    assert stake==pytest.approx(10/(.0216*20))
    assert stake*20*.0216==pytest.approx(10)


def test_unlimited_count_uses_numeric_stake_and_preserves_old_trade_plan(strategy):
    assert strategy.config['max_open_trades']==-1
    assert isinstance(strategy.config['stake_amount'],(int,float))
    strategy.executor.shutdown();strategy.executor=Mock()
    strategy.wallets=SimpleNamespace(get_total_stake_amount=lambda:3000,get_available_stake_amount=lambda:75)
    strategy.bot_loop_start(datetime.now(timezone.utc))
    assert strategy.config['stake_amount']==75
    trade=SimpleNamespace(id=7,enter_tag='jev:old:98:104',is_short=False,leverage=1,open_date_utc=datetime.now(timezone.utc))
    assert strategy.custom_stoploss('BTC/USDT:USDT',trade,datetime.now(timezone.utc),100,0)==pytest.approx(.02)


def test_upgrade_restores_tighter_stop_after_freqtrade_reinitializes(strategy):
    now=datetime.now(timezone.utc)
    trade=Trade(pair='BTC/USDT:USDT',exchange='binance',enter_tag='jev:old:90:120',
                open_rate=100,stake_amount=140,amount=1.4,is_open=True,open_date=now,
                fee_open=.0005,fee_close=.0005,leverage=1,is_short=False,
                stop_loss=95,initial_stop_loss=95,initial_stop_loss_pct=-.05,
                is_stop_loss_trailing=False,price_precision=8,precision_mode_price=2)
    Trade.session.add(trade);Trade.commit()
    strategy.preserved_stops={trade.id:95}
    Trade.stoploss_reinitialization(-.5)
    assert trade.stop_loss==50
    strategy.executor.shutdown();strategy.executor=Mock()
    strategy.wallets=SimpleNamespace(get_total_stake_amount=lambda:2000,get_available_stake_amount=lambda:1860)
    strategy.bot_loop_start(now)
    assert trade.stop_loss==95
    assert strategy.custom_stoploss(trade.pair,trade,now,100,0)==pytest.approx(.05)
