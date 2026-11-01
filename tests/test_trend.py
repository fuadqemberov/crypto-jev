"""Trend portfolio: Donchian state, no look-ahead, sizing caps, costs, long-only."""
import numpy as np
import pandas as pd

from backtest.trend import TrendParams, donchian_state, evaluate, run, target_weights

D = 86_400_000
T0 = 1_700_000_000_000 // D * D


def frame(close, vol=1e6):
    close = np.asarray(close, float)
    idx = pd.Index(T0 + np.arange(len(close)) * D, name='time')
    return pd.DataFrame(dict(open=close, high=close * 1.01, low=close * 0.99, close=close, volume=vol), index=idx)


def noisy(n, drift, seed):
    r = np.random.default_rng(seed).normal(drift, 0.03, n)
    return 100 * np.exp(np.cumsum(r))


def params(**kw):
    return TrendParams(**{**dict(lookbacks_days=(10, 20), vol_days=10, turnover_days=10, top=3), **kw})


def test_donchian_breakout_and_exit():
    c = np.r_[np.full(30, 100.0), np.linspace(101, 130, 20), np.linspace(129, 90, 30)]
    s = donchian_state(c, c, c, 10)
    assert s[:10].tolist() == [0] * 10
    assert s[31] == 1                     # close above the prior 10-bar high
    assert -1 in s[60:]                   # the fall ends below the 10-bar low
    first_flat = 50 + np.flatnonzero(s[50:] != 1)[0]
    assert s[first_flat] in (0, -1)


def test_no_look_ahead():
    data = {f'C{i}USDT': frame(noisy(200, 0.002 * (i - 1), i)) for i in range(4)}
    w1, _, _ = target_weights(data, params(), D)
    changed = {s: df.copy() for s, df in data.items()}
    for df in changed.values():
        df.iloc[150:, :4] *= 3.0          # rewrite the future
    w2, _, _ = target_weights(changed, params(), D)
    assert np.allclose(w1[:150], w2[:150])


def test_caps_and_long_only():
    data = {f'C{i}USDT': frame(noisy(200, -0.01 if i % 2 else 0.01, i)) for i in range(6)}
    p = params(max_weight=0.3, max_gross=1.0, top=6)
    w, _, _ = target_weights(data, p, D)
    assert np.abs(w).max() <= 0.3 + 1e-9 and np.abs(w).sum(axis=1).max() <= 1.0 + 1e-9
    assert (w < 0).any()
    wl, _, _ = target_weights(data, params(long_only=True, top=6), D)
    assert (wl >= 0).all() and (wl > 0).any()


def test_universe_is_top_by_turnover():
    data = {f'C{i}USDT': frame(noisy(200, 0.01, i), vol=10 ** (i + 3)) for i in range(5)}
    w, _, syms = target_weights(data, params(top=2), D)
    held = {syms[j] for j in np.flatnonzero(np.abs(w).sum(axis=0))}
    assert held <= {'C3USDT', 'C4USDT'}


def test_trend_earns_and_costs_hurt():
    data = {f'C{i}USDT': frame(noisy(300, 0.01, i)) for i in range(3)}
    free = run(data, params(cost_rate=0.0, funding_8h=0.0), D)
    paid = run(data, params(cost_rate=0.005, funding_8h=0.001), D)
    assert free.equity[-1] > 2000 and paid.equity[-1] < free.equity[-1]
    assert paid.fees > 0 and paid.funding > 0 and free.fills == paid.fills
    row = evaluate(free, 2000.0, 0.33)
    assert row['return_pct'] > 0 and row['sharpe'] > 0 and 0 <= row['max_dd_pct'] < 100


def test_flat_market_does_nothing():
    data = {f'C{i}USDT': frame(np.full(200, 100.0)) for i in range(3)}
    r = run(data, params(), D)
    assert r.fills == 0 and r.equity[-1] == 2000.0
