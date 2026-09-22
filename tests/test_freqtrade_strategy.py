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
    config = build_config(Settings(), 'x'*32, 'test', 'test-password')
    config.update(runmode=RunMode.DRY_RUN, user_data_dir=ROOT/'user_data',
                  strategy_path=str(ROOT/'user_data/strategies'))
    instance = StrategyResolver.load_strategy(config)
    validate_config_consistency(config)
    instance.bot_start()
    instance.dp = SimpleNamespace(orderbook=lambda pair, depth: {'bids': [[99.995, 1]], 'asks': [[100.005, 1]]})
    yield instance
    instance.executor.shutdown(wait=True, cancel_futures=True)


def signal(action='LONG'):
    now = datetime.now(timezone.utc)
    return dict(id='a'*24, pair='BTC/USDT:USDT', action=action,
                observed_at=int(now.timestamp()*1000)-100, expires_at=int(now.timestamp()*1000)+120000,
                levels=dict(entry=100, stop=98 if action=='LONG' else 102, target=104 if action=='LONG' else 96))


def load(strategy, value, enabled=True):
    now=datetime.now(timezone.utc)
    strategy._accept(dict(version=1, mode='dry_run', generated_at=int(now.timestamp()*1000),
                          entries_enabled=enabled, signals=[value]), now.timestamp()*1000)
    return now


def tag(value):
    return f"jev:{value['id']}:{value['levels']['stop']}:{value['levels']['target']}"


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
