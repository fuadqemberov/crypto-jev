"""Futures backtest: live rules on history, intrabar stop / TP, out-of-sample and walk-forward."""
import numpy as np
import pandas as pd
import pytest

from app.config import Settings
from backtest.futures_sim import SimResult, metrics, out_of_sample, simulate, walk_forward
from backtest.strategies import STRATEGIES, Strategy

H = 3_600_000
T0 = 1_790_000_000_000 // H * H


def flat(n=120, price=100.0):
    idx = pd.Index(T0 + np.arange(n) * H, name='time')
    return pd.DataFrame(dict(open=price, high=price + 0.5, low=price - 0.5, close=price, volume=1000.0), index=idx)


def long_from(bar):
    def fn(df):
        pos = np.zeros(len(df))
        if df.close.iloc[-1] > 1:                 # real series only; the mirrored one stays flat
            pos[bar:] = 1.0
        return pos
    return Strategy('tst', 'Test', 'Test', '', fn)


def settings(**kw):
    return Settings(**{**dict(capital=2000.0, margin_pct=5.0, take_profit=0.0, tp_r_multiple=1.5, max_cost_r=0.0,
                              fee_rate=0.0005, slippage_rate=0.0005), **kw})


def run(df, **kw):
    r = simulate([long_from(80)], {'AUSDT': df}, settings(**kw))['tst']
    return r, r.trades[0]


def test_entry_is_the_next_open_after_the_signal_bar():
    df = flat()
    df.loc[df.index[81]:, 'open'] = 101.0                  # the bar after the signal opens higher
    r, t = run(df)
    assert t['opened_at'] == int(df.index[81]) and t['entry_price'] == 101.0


@pytest.mark.parametrize('case', ['stop', 'tp', 'both', 'gap'])
def test_intrabar_stop_and_take_profit(case):
    df = flat()
    i = df.index[90]
    if case == 'stop':
        df.loc[i, 'low'] = 90.0
    elif case == 'tp':
        df.loc[i, 'high'] = 110.0
    elif case == 'both':
        df.loc[i, ['low', 'high']] = [90.0, 110.0]
    else:
        df.loc[i, ['open', 'low', 'close']] = [92.0, 91.5, 92.0]   # opens below the stop
    r, t = run(df)
    if case == 'tp':
        assert t['reason'] == 'take_profit' and t['pnl'] > 0
        assert 100 < t['exit_price'] < 110                 # filled at the 1.5R level inside the wick, not at the high
    else:
        assert t['reason'] == 'stop_loss' and t['pnl'] < 0
        if case == 'gap':
            assert t['exit_price'] == 92.0                  # filled at the gap open, not at the stop
        else:
            assert t['exit_price'] > 92.0                   # filled at the stop level
    assert r.equity[-1] == pytest.approx(2000 + sum(x['pnl'] for x in r.trades), rel=1e-9)


def test_take_profit_is_one_and_a_half_r_and_stop_risk_is_about_a_quarter_margin():
    df = flat()
    df.loc[df.index[90], 'high'] = 110.0
    _, win = run(df)
    df = flat()
    df.loc[df.index[90], 'low'] = 90.0
    _, loss = run(df)
    assert -loss['pnl'] == pytest.approx(0.25 * loss['margin'], rel=0.25)   # RISK_PER_MOVE 0.25
    assert win['pnl'] > 1.2 * -loss['pnl']                                  # 1.5R gross, fees on top


def test_metrics_and_robustness_tables():
    n = 400
    up = SimResult('up', 'Up', 2000 * np.linspace(1, 1.4, n))
    down = SimResult('down', 'Down', 2000 * np.linspace(1, 0.7, n))
    flip = SimResult('flip', 'Flip', 2000 * np.concatenate([np.linspace(1, 1.3, n // 2), np.linspace(1.3, 1.0, n // 2)]))
    res = {r.key: r for r in (up, down, flip)}
    oos = out_of_sample(res, 200)
    assert list(oos.key) == ['flip', 'up', 'down'] and oos.loc[0, 'out_sample_pct'] < 0
    wf = walk_forward(res, train=100, test=50, top=1, by='return')
    assert len(wf) == 6 and (wf.picked == 'up').sum() >= 2
    m = metrics(up.equity, [], 2000)
    assert m['return_pct'] == pytest.approx(40) and m['max_dd_pct'] == pytest.approx(0)


def test_all_live_rules_run_and_reconcile():
    rng = np.random.default_rng(4)
    data = {}
    for k, sym in enumerate(('AUSDT', 'BUSDT', 'CUSDT')):
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, 900)))
        o = np.concatenate([[100], close[:-1]])
        idx = pd.Index(T0 + np.arange(900) * H, name='time')
        data[sym] = pd.DataFrame(dict(open=o, high=np.maximum(o, close) * 1.004, low=np.minimum(o, close) * 0.996,
                                      close=close, volume=rng.uniform(100, 1000, 900)), index=idx)
    res = simulate(STRATEGIES, data, settings(max_cost_r=0.10))
    assert set(res) == {s.key for s in STRATEGIES}
    reasons = {t['reason'] for r in res.values() for t in r.trades}
    assert reasons <= {'take_profit', 'stop_loss', 'liquidation', 'signal', 'reverse', 'timeout', 'end'}
    assert sum(len(r.trades) for r in res.values()) > 100
    for r in res.values():
        assert r.equity[-1] == pytest.approx(2000 + sum(t['pnl'] for t in r.trades), rel=1e-9)
        assert all(1 <= t['leverage'] <= 100 for t in r.trades)


def test_only_the_most_liquid_symbols_are_downloaded():
    from backtest.futures_sim import liquid_symbols

    class Fake:
        def symbols(self):
            return ['AUSDT', 'BUSDT', 'CUSDT', 'DUSDT']

        def _get(self, path):
            assert path == '/ticker/24hr'
            return [{'symbol': 'AUSDT', 'quoteVolume': '5'}, {'symbol': 'BUSDT', 'quoteVolume': '50'},
                    {'symbol': 'CUSDT', 'quoteVolume': '20'}, {'symbol': 'XUSDC', 'quoteVolume': '999'},
                    {'symbol': 'DUSDT', 'quoteVolume': '1'}]
    assert liquid_symbols(Fake(), 2) == ['BUSDT', 'CUSDT', 'AUSDT']   # 1.5 x 2 = 3, stablecoin pair excluded


# --- experiment options -------------------------------------------------------------------------
def walk(n=1200, seed=4, syms=('AUSDT', 'BUSDT')):
    rng = np.random.default_rng(seed)
    out = {}
    for sym in syms:
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
        o = np.concatenate([[100], close[:-1]])
        idx = pd.Index(T0 + np.arange(n) * H, name='time')
        out[sym] = pd.DataFrame(dict(open=o, high=np.maximum(o, close) * 1.004, low=np.minimum(o, close) * 0.996,
                                     close=close, volume=rng.uniform(100, 1000, n)), index=idx)
    return out


def test_resample_to_4h_is_exact():
    from backtest.futures_sim import resample
    d = walk(n=50)
    r = resample(d, 4 * H)['AUSDT']
    src = d['AUSDT']
    first = src.loc[r.index[0]:r.index[0] + 3 * H]
    assert len(first) == 4 and np.all(np.diff(r.index) == 4 * H) and int(r.index[0]) % (4 * H) == 0
    assert r.iloc[0].open == first.open.iloc[0] and r.iloc[0].close == first.close.iloc[-1]
    assert r.iloc[0].high == first.high.max() and r.iloc[0].low == first.low.min()
    assert r.iloc[0].volume == pytest.approx(first.volume.sum())


def test_side_filter_and_stop_tp_only_mode():
    from dataclasses import replace
    data = walk()
    rules = [s for s in STRATEGIES if s.key in ('sma_cross', 'rsi', 'donchian')]
    shorts = simulate(rules, data, settings(), side='short')
    assert all(t['side'] == -1 for r in shorts.values() for t in r.trades) and any(r.trades for r in shorts.values())
    longs = simulate(rules, data, settings(), side='long')
    assert all(t['side'] == 1 for r in longs.values() for t in r.trades)
    none = simulate(rules, data, replace(settings(), signal_exits='none'))
    assert not {t['reason'] for r in none.values() for t in r.trades} & {'signal', 'reverse'}


def test_fixed_margin_keeps_trading_after_the_account_is_drained():
    from backtest.futures_sim import out_of_sample
    data = walk(n=1500)
    rules = [s for s in STRATEGIES if s.key in ('macd', 'psar')]
    res = simulate(rules, data, settings(), fixed_margin=100.0)
    for r in res.values():
        assert r.fixed and all(t['margin'] == pytest.approx(100.0) for t in r.trades)
        assert r.equity[-1] == pytest.approx(2000 + sum(t['pnl'] for t in r.trades), rel=1e-9)
        late = [t for t in r.trades if t['opened_at'] >= int(r.times[1000])]
        assert late                                                      # still trading in the last third
    o = out_of_sample(res, 1000)
    assert (o.oos_trades > 0).all() and set(o.columns) >= {'is_pf', 'oos_pf', 'is_trades', 'oos_trades'}


def test_experiments_run_end_to_end(tmp_path, monkeypatch):
    import zipfile
    from backtest import experiments
    monkeypatch.setattr(experiments.BinanceData, 'load_all', lambda self, a, b, c, d: (walk(n=600), []))
    monkeypatch.setattr(experiments, 'liquid_symbols', lambda source, n: ['AUSDT', 'BUSDT'])
    monkeypatch.setenv('HOME', str(tmp_path))
    out = tmp_path / 'exp'
    experiments.main(['--days', '25', '--top', '2', '--out', str(out), '--train-days', '5', '--test-days', '2',
                      '--only', 'base', 'short_stoptp_4h'])
    cmp_df = pd.read_csv(out / 'comparison.csv')
    assert list(cmp_df.variant) == ['base', 'short_stoptp_4h']
    assert (out / 'base' / 'summary.csv').exists() and (out / 'short_stoptp_4h' / 'walk_forward.csv').exists()
    zips = list(tmp_path.glob('experiments-*.zip'))
    assert zips and 'comparison.csv' in zipfile.ZipFile(zips[0]).namelist()
