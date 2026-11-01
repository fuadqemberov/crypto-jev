"""Live trend forward test: one rebalance per closed 6h bar, long only, costs, persistence."""
import asyncio

import numpy as np
import pandas as pd

from app.config import Settings
from app.trend_paper import BAR_MS, HISTORY, PARAMS, TrendPaper

T0 = 1_790_000_000_000 // BAR_MS * BAR_MS


def frames(n_extra=0, seed=0):
    out = {}
    n = HISTORY + n_extra
    for i in range(25):
        rng = np.random.default_rng(seed * 100 + i)
        c = 100 * np.exp(np.cumsum(rng.normal(0.004 if i % 3 else -0.004, 0.02, n)))
        idx = pd.Index(T0 + np.arange(n) * BAR_MS, name='time')
        df = pd.DataFrame(dict(open=c, high=c * 1.01, low=c * 0.99, close=c, volume=1e6 * (i + 1)), index=idx)
        out['BTCUSDT' if i == 0 else f'C{i}USDT'] = df.tail(HISTORY)
    return out


def paper(tmp_path):
    return TrendPaper(Settings(data_dir=tmp_path), market=None)


def test_rebalance_long_only_within_caps(tmp_path):
    tp = paper(tmp_path)
    tp.apply(frames())
    st = tp.state
    assert st['fills'] > 0 and st['fees'] > 0 and st['equity'] < 2000
    assert all(w > 0 for w in st['held'].values())
    assert max(st['held'].values()) <= PARAMS.max_weight + 1e-9 and sum(st['held'].values()) <= PARAMS.max_gross + 1e-9
    assert len(st['universe']) == PARAMS.top and set(st['held']) <= set(st['universe'])


def test_same_bar_is_ignored_and_next_bar_marks_to_market(tmp_path):
    tp = paper(tmp_path)
    tp.apply(frames())
    snap = dict(tp.state)
    tp.apply(frames())                                  # same bar again: nothing happens
    assert tp.state['equity'] == snap['equity'] and len(tp.state['history']) == 1
    tp.apply(frames(n_extra=1))
    assert len(tp.state['history']) == 2 and tp.state['equity'] != snap['equity']
    assert tp.state['funding'] > 0


def test_state_survives_restart(tmp_path):
    tp = paper(tmp_path)
    tp.apply(frames())
    tp._save()
    again = paper(tmp_path)
    assert again.state['equity'] == tp.state['equity'] and again.state['held'] == tp.state['held']
    s = again.summary()
    assert s['positions'] and s['curve'] and s['btc_curve'] and s['next_run'] == s['last_bar'] + 2 * BAR_MS


def test_step_uses_market(tmp_path):
    data = frames()

    class Market:
        def now_ms(self):
            return T0 + (HISTORY + 1) * BAR_MS + 120_000

        async def universe(self):
            return [(s, 1.0) for s in data]

        async def _get(self, path, symbol, interval, limit):
            assert interval == '6h'
            df = data[symbol]
            return [[int(t), r.open, r.high, r.low, r.close, r.volume] for t, r in df.iterrows()]

    tp = TrendPaper(Settings(data_dir=tmp_path), Market())
    asyncio.run(tp.step())
    assert tp.state['last_bar'] == int(data['BTCUSDT'].index[-1]) and (tmp_path / 'trend-paper.json').exists()
