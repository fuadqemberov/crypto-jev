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


def test_top5_plus_consensus_rule():
    assert [s.key for s in STRATEGIES] == ['ema_pullback', 'williams', 'adx', 'rr_2to1', 'consensus3']


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
    assert len(results) == len(STRATEGIES)
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


# --- member tactics --------------------------------------------------------------------
def _by_key(key):
    from backtest.strategies import MEMBERS
    return next(s for s in [*MEMBERS, *STRATEGIES] if s.key == key)


def _flat(n=40):
    idx = pd.Index(np.arange(n) * HOUR, name='time')
    return pd.DataFrame(dict(open=100.0, high=101.0, low=99.0, close=100.0, volume=500.0), index=idx)


def test_rr_2to1_exits_only_at_minus_half_or_plus_one_percent():
    df = frame(3000, seed=4)
    pos = _by_key('rr_2to1').fn(df)
    change = np.diff(np.concatenate([[0.0], pos, [0.0]]))
    entries, exits = np.flatnonzero(change > 0), np.flatnonzero(change < 0)
    assert len(entries) > 5
    c = df.close.to_numpy()
    for e, x in zip(entries, exits):
        if x < len(c):
            r = c[x] / c[e] - 1
            assert r <= -0.005 + 1e-12 or r >= 0.01 - 1e-12


def _entries_exits(pos):
    change = np.diff(np.concatenate([[0.0], pos, [0.0]]))
    return np.flatnonzero(change > 0), np.flatnonzero(change < 0)


def test_ema_pullback_respects_session_filter_and_one_stop_per_day():
    from backtest.strategies import DAY_MS, _near_session_open
    df = frame(4000, seed=8)
    pos = _by_key('ema_pullback').fn(df)
    entries, exits = _entries_exits(pos)
    assert len(entries) > 5
    assert not _near_session_open(df.index[entries]).any()
    day = np.asarray(df.index, dtype='int64') // DAY_MS
    for e, x in zip(entries[1:], exits[:-1]):          # pos[x] == 0: the trade ended on bar x
        assert day[e] != day[x]                         # ...so no new entry on that UTC day


def test_consensus_enters_on_three_votes_and_holds_while_two_agree():
    from backtest.strategies import member_votes
    df = frame(4000, seed=3)
    votes = member_votes(df)
    pos = _by_key('consensus3').fn(df)
    entries = np.flatnonzero(np.diff(np.concatenate([[0.0], pos])) > 0)
    assert all(votes[i] >= 3 for i in entries)
    assert (votes[pos > 0.5] >= 2).all()
