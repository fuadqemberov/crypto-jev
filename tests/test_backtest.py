import numpy as np
import pandas as pd
import pytest

from backtest.engine import metrics, run_all, simulate_coin
from backtest.strategies import STRATEGIES, state

HOUR = 3_600_000


def frame(n=600, seed=1):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    open_ = np.concatenate([[100], close[:-1]]) * (1 + rng.normal(0, 0.001, n))
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.005, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.005, n))
    vol = rng.uniform(100, 1000, n)
    idx = pd.Index(np.arange(n) * HOUR + 1_700_000_000_000 // HOUR * HOUR, name='time')
    return pd.DataFrame(dict(open=open_, high=high, low=low, close=close, volume=vol), index=idx)


def test_twenty_unique_strategies():
    assert len(STRATEGIES) == 20
    assert len({s.key for s in STRATEGIES}) == 20


def test_buy_and_hold_matches_closed_form():
    df = frame()
    cost = 0.0015
    r = simulate_coin(df, np.ones(len(df)), 1000, cost)
    # Signal at close of bar 0 -> filled at open of bar 1, liquidated at final close.
    expected = 1000 * df.close.iloc[-1] / df.open.iloc[1] * (1 - cost) ** 2
    assert r.equity[-1] == pytest.approx(expected, rel=1e-9)
    assert len(r.trade_returns) == 1
    assert r.trade_pnl[0] == pytest.approx(r.equity[-1] - 1000, rel=1e-9)


def test_trade_pnl_reconciles_with_equity():
    df = frame(seed=3)
    pos = (np.arange(len(df)) // 37) % 2
    r = simulate_coin(df, pos, 500, 0.002)
    assert r.trade_pnl.sum() == pytest.approx(r.equity[-1] - 500, rel=1e-9)


def test_flat_position_keeps_capital():
    r = simulate_coin(frame(), np.zeros(600), 750, 0.001)
    assert np.allclose(r.equity, 750) and len(r.trade_returns) == 0


def test_no_lookahead():
    """Changing future bars must not change past positions of any strategy."""
    df = frame(800, seed=5)
    changed = df.copy()
    changed.iloc[700:, :4] *= 1.5
    changed['high'] = changed[['open', 'high', 'close']].max(axis=1)
    for s in STRATEGIES:
        a, b = s.fn(df), s.fn(changed)
        assert np.array_equal(a[:700], b[:700]), s.key


def test_state_exit_wins():
    idx = pd.RangeIndex(4)
    pos = state(pd.Series([True, False, True, False], index=idx), pd.Series([False, True, True, False], index=idx))
    assert pos.tolist() == [1, 0, 0, 0]


def test_run_all_ranks_and_metrics():
    data = {f'C{i}USDT': frame(500, seed=i) for i in range(3)}
    results, bench = run_all(data, 2000, 0.0015)
    assert len(results) == 20
    rets = [r.metrics['total_return'] for r in results]
    assert rets == sorted(rets, reverse=True)
    for r in results + [bench]:
        assert r.equity.iloc[0] == pytest.approx(2000, rel=0.01)
        assert 0 <= r.metrics['max_drawdown'] <= 100
        assert r.metrics['coin_count'] == 3


def test_metrics_drawdown():
    eq = pd.Series([2000, 2200, 1100, 1650.0])
    m = metrics(eq, {}, 2000)
    assert m['max_drawdown'] == pytest.approx(50)
    assert m['total_return'] == pytest.approx(-17.5)
