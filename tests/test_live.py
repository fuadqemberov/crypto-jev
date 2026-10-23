"""Live consensus-branch USDT-M futures paper engine: accounting, signals, TP, liquidation, funding, API."""
from __future__ import annotations

import asyncio
import base64
import math

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.demo import DemoMarket
from app.engine import Engine
from app.futures import LEVERAGE_PROFILE, direction, evaluate, leverage, mirror
from app.ledger import Ledger
from app.main import create_app
from app.market import Quote
from app.store import Store
from backtest.strategies import STRATEGIES, Strategy

H = 3_600_000
T0 = 1_790_000_000_000 // H * H
F, S = 0.0005, 0.0005


def settings(**kw) -> Settings:
    base = dict(capital=2000.0, margin_pct=5.0, take_profit=15.0, tp_r_multiple=0.0,
                signal_exits='all', cooldown_bars=0, max_cost_r=0.0, max_hold_hours=0.0, fee_rate=F, slippage_rate=S,
                maint_margin_rate=0.005, candle_delay_seconds=0)
    return Settings(**{**base, **kw})


def ledger(**kw) -> Ledger:
    return Ledger(settings(**kw), Store(':memory:'), ['a'], T0)


# --- ledger accounting --------------------------------------------------------------
def test_long_open_close_math_and_equity_identity():
    led = ledger()
    pos = led.open('a', 'XUSDT', 1, 10, 100.0, T0)
    assert pos.margin == pytest.approx(100.0)                     # 5 % of 2000
    assert pos.qty == pytest.approx(1000 / (100 * (1 + S)))
    assert pos.fee == pytest.approx(1000 * F)
    assert led.accounts['a'].cash == pytest.approx(2000 - 100 - 0.5)
    led.update_prices({'XUSDT': 101.0})
    exit_fill = 101 * (1 - S)
    expected = 100 + pos.qty * (exit_fill - pos.fill_price) - pos.qty * exit_fill * F
    assert led.close_value(pos) == pytest.approx(expected)
    assert led.equity('a') == pytest.approx(led.accounts['a'].cash + expected)
    t = led.close('a', 'XUSDT', 101.0, T0 + H, 'signal')
    assert t['pnl'] == pytest.approx(expected - 100 - 0.5)
    assert t['ret'] == pytest.approx(t['pnl'] / 100)
    assert led.accounts['a'].cash == pytest.approx(2000 + t['pnl'])


def test_short_profits_when_price_falls_and_loses_when_it_rises():
    led = ledger()
    pos = led.open('a', 'XUSDT', -1, 5, 100.0, T0)
    assert pos.fill_price == pytest.approx(100 * (1 - S))
    assert led.net_pnl(pos, 95.0) > 0 > led.net_pnl(pos, 105.0)
    gross = -pos.qty * (95 * (1 + S) - pos.fill_price)
    assert led.close_value(pos, 95.0) == pytest.approx(100 + gross - pos.qty * 95 * (1 + S) * F)


def test_liquidation_price_matches_maintenance_condition():
    led = ledger()
    for side in (1, -1):
        pos = led.open('a', f'{side}USDT'.replace('-', 'M'), side, 20, 100.0, T0)
        liq = led.liquidation_price(pos)
        assert pos.margin + led.unrealized(pos, liq) == pytest.approx(0.005 * pos.qty * liq)
        assert not led.is_liquidated(pos, 100.0)
        assert led.is_liquidated(pos, liq * (1 - side * 1e-6))
        assert abs(liq / 100 - 1) < 1 / 20                           # inside 1/leverage
    t = led.close('a', '1USDT', 90.0, T0, 'liquidation')
    assert t['proceeds'] == 0 and t['pnl'] == pytest.approx(-100 - t['fees'])


def test_funding_long_pays_short_receives():
    led = Ledger(settings(), Store(':memory:'), ['a', 'b'], T0)
    lp = led.open('a', 'XUSDT', 1, 10, 100.0, T0)
    sp = led.open('b', 'XUSDT', -1, 10, 100.0, T0)
    paid = led.apply_funding('XUSDT', 0.001, 100.0, T0 + 8 * H)
    assert lp.funding == pytest.approx(lp.qty * 100 * 0.001) and lp.margin == pytest.approx(100 - lp.funding)
    assert sp.funding == pytest.approx(-sp.qty * 100 * 0.001)
    assert paid == pytest.approx(lp.funding + sp.funding)
    t = led.close('a', 'XUSDT', 100.0, T0 + 9 * H, 'signal')
    assert t['funding'] == pytest.approx(lp.funding) and t['margin'] == pytest.approx(100)
    # funding before the position existed is not charged
    led.open('a', 'YUSDT', 1, 10, 50.0, T0 + 10 * H)
    assert led.apply_funding('YUSDT', 0.01, 50.0, T0 + 8 * H) == 0


def test_unlimited_parallel_positions_until_margin_runs_out():
    led = ledger(margin_pct=2.0)
    opened = sum(1 for i in range(200) if led.open('a', f'C{i}USDT', 1 if i % 2 else -1, 10, 10.0, T0))
    assert opened > 40                                              # far beyond any slot limit
    assert led.accounts['a'].cash < settings().min_margin * (1 + 10 * F)
    assert led.open('a', 'NEWUSDT', 1, 10, 10.0, T0) is None        # only free margin limits it
    assert led.open('a', 'C1USDT', 1, 10, 10.0, T0) is None         # one position per coin


def test_ledger_persists_across_restart(tmp_path):
    s = settings()
    store = Store(tmp_path / 'x.db')
    led = Ledger(s, store, ['a'], T0)
    led.open('a', 'XUSDT', -1, 7, 100.0, T0)
    led.apply_funding('XUSDT', 0.0002, 100.0, T0 + H)
    store.close()
    led2 = Ledger(s, Store(tmp_path / 'x.db'), ['a'], T0 + 5 * H)
    pos = led2.accounts['a'].positions['XUSDT']
    assert (pos.side, pos.leverage) == (-1, 7) and pos.funding < 0
    assert led2.accounts['a'].started_at == T0


# --- strategy layer -------------------------------------------------------------------
def test_leverage_is_chosen_by_strategy_and_volatility_from_1x_to_100x():
    assert leverage('williams', 0.004, 100) == 31                    # floor(0.25 / 0.008)
    assert leverage('adx', 0.004, 100) == 20                         # floor(0.25 / 0.012)
    assert leverage('adx', 0.04, 100) == 2                           # floor(0.25 / 0.12)
    assert leverage('williams', 0.004, 100, 0.5) == 62               # RISK_PER_MOVE=0.5 doubles it
    assert leverage('williams', 0.0005, 100) == 100                  # very calm coin -> capped at 100x
    assert leverage('confluence', 0.0005, 100) == 5                  # its own 5x ceiling
    assert leverage('adx', 0.5, 100) == 1                            # never below 1x
    assert leverage('williams', 0.0005, 20) == 20                    # MAX_LEVERAGE wins
    assert leverage('williams', float('nan'), 100) == 1
    assert settings().max_leverage == 100
    from backtest.strategies import MEMBERS
    assert set(LEVERAGE_PROFILE) == {s.key for s in [*MEMBERS, *STRATEGIES]}


def test_mirror_turns_long_rules_into_short_rules():
    rng = np.random.default_rng(1)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 400)))
    df = pd.DataFrame({'open': close, 'high': close * 1.003, 'low': close * 0.997, 'close': close, 'volume': 1.0},
                      index=pd.Index(T0 + np.arange(400) * H, name='time'))
    m = mirror(df)
    assert (m.high >= m.low).all() and np.allclose(mirror(m).close, df.close)
    ema = Strategy('ema', 'EMA', 'Test', '', lambda df: (df.close.ewm(span=12).mean() > df.close.ewm(span=26).mean()).astype(float).to_numpy())
    d = direction(ema, df)
    assert set(np.unique(d)) <= {-1, 0, 1} and (d == 1).any() and (d == -1).any()


def test_evaluate_isolates_a_failing_strategy():
    boom = Strategy('boom', 'Boom', 'Test', '', lambda df: 1 / 0)
    df = pd.DataFrame({'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': [99.0] * 69 + [101.0], 'volume': 1.0})
    out = evaluate([boom, DIR], {'AUSDT': df})
    assert out['boom'] == {} and (out['dir']['AUSDT'].prev, out['dir']['AUSDT'].cur) == (0, 1)


# --- engine with a scripted market ----------------------------------------------------
def _dir(df):
    if df.close.iloc[-1] > 1:                       # real series: long above 100
        return (df.close > 100).astype(float).to_numpy()
    return ((1 / df.close) < 95).astype(float).to_numpy()   # mirrored series: short below 95


DIR = Strategy('dir', 'Dir', 'Test', 'long > 100, short < 95', _dir)


class ScriptedMarket:
    demo = False

    def __init__(self, closes: dict[str, list[float]], volume: dict[str, float]):
        self.closes, self.volume = closes, volume
        self.bar = 0
        self.live: dict[str, float] = {}          # intra-candle mark overrides
        self.lows: dict[str, float] = {}
        self.funding: dict[str, tuple[float, int]] = {}
        self.offset = 60_000

    def now_ms(self):
        return T0 + (self.bar + 1) * H + self.offset

    async def universe(self):
        return sorted(self.volume.items(), key=lambda x: -x[1])

    async def quotes(self):
        out = {}
        for s, c in self.closes.items():
            rate, nxt = self.funding.get(s, (0.0, 0))
            out[s] = Quote(self.live.get(s, c[self.bar]), rate, nxt)
        return out

    async def klines(self, symbol, limit):
        c = np.array(self.closes[symbol][:self.bar + 1], dtype=float)
        close = np.concatenate([np.full(100, 97.0), c])
        idx = T0 + np.arange(-100, len(c)) * H                   # last bar == the candle that just closed
        df = pd.DataFrame({'open': close, 'high': close * 1.001, 'low': close * 0.999, 'close': close, 'volume': 1.0},
                          index=pd.Index(idx.astype('int64'), name='time'))
        if symbol in self.lows:
            df.iloc[-1, df.columns.get_loc('low')] = self.lows[symbol]
        if symbol in getattr(self, 'highs', {}):
            df.iloc[-1, df.columns.get_loc('high')] = self.highs[symbol]
        return df.tail(limit)


def tick(engine, market, bar=None):
    if bar is not None:
        market.bar = bar
    asyncio.run(engine.tick())


def test_long_entry_take_profit_and_no_reentry_until_fresh_signal():
    m = ScriptedMarket({'AUSDT': [101, 101, 101, 96, 101]}, {'AUSDT': 1})
    e = Engine(settings(), m, Store(':memory:'), [DIR])
    tick(e, m, 0)
    pos = e.ledger.accounts['dir'].positions['AUSDT']
    assert pos.side == 1 and 1 <= pos.leverage <= 100
    assert e.signals['dir']['entries'] == [{'symbol': 'AUSDT', 'side': 'LONG', 'leverage': pos.leverage}]
    # price moves enough for > $15 net profit between candles -> closed by take profit
    need = 101 * (1 + (15 + pos.fee + 2) / pos.notional * 1.2)
    m.live['AUSDT'] = need
    m.offset = 20 * 60_000
    tick(e, m)
    t = e.store.trades('dir')[0]
    assert t['reason'] == 'take_profit' and t['pnl'] >= 15
    m.live.clear()
    tick(e, m, 1)                                   # state still LONG: no new position
    assert e.ledger.accounts['dir'].positions == {}
    tick(e, m, 3)                                   # 96: flat
    tick(e, m, 4)                                   # back above 100: fresh LONG
    assert e.ledger.accounts['dir'].positions['AUSDT'].side == 1


def test_reverse_long_to_short_on_the_same_candle():
    m = ScriptedMarket({'AUSDT': [101, 90]}, {'AUSDT': 1})
    e = Engine(settings(take_profit=0.0), m, Store(':memory:'), [DIR])
    tick(e, m, 0)
    tick(e, m, 1)
    assert e.store.trades('dir')[0]['reason'] in ('reverse', 'liquidation')
    pos = e.ledger.accounts['dir'].positions['AUSDT']
    assert pos.side == -1
    assert e.signals['dir']['short'] == ['AUSDT']


def test_liquidation_on_price_poll_and_on_candle_wick():
    m = ScriptedMarket({'AUSDT': [101, 101, 101], 'BUSDT': [101, 101, 101]}, {'AUSDT': 2, 'BUSDT': 1})
    e = Engine(settings(take_profit=0.0, stop_loss_pct=0.0), m, Store(':memory:'), [DIR])
    tick(e, m, 0)
    a = e.ledger.accounts['dir'].positions['AUSDT']
    b = e.ledger.accounts['dir'].positions['BUSDT']
    m.live['AUSDT'] = e.ledger.liquidation_price(a) * 0.999
    m.offset = 10 * 60_000
    tick(e, m)
    assert 'AUSDT' not in e.ledger.accounts['dir'].positions
    m.live.clear()
    m.offset = 60_000
    tick(e, m, 1)                                   # candle in which B was opened: its wick is not used
    assert 'BUSDT' in e.ledger.accounts['dir'].positions
    m.lows['BUSDT'] = e.ledger.liquidation_price(b) * 0.99      # wick only visible in the closed candle
    tick(e, m, 2)
    trades = {t['symbol']: t for t in e.store.trades('dir')}
    assert trades['AUSDT']['reason'] == trades['BUSDT']['reason'] == 'liquidation'
    assert trades['BUSDT']['proceeds'] == 0


def test_stop_price_is_atr_based_and_capped_before_liquidation():
    led = ledger(stop_loss_pct=50.0)
    for side in (1, -1):
        for lev in (1, 10, 100):
            cap_stop = led.stop_price(side, 100.0, lev)                     # no ATR -> margin cap
            assert cap_stop == pytest.approx(100 - side * 50 / lev)
            assert led.stop_price(side, 100.0, lev, 0.1) == pytest.approx(100 - side * 0.1)   # tighter ATR stop wins
            assert led.stop_price(side, 100.0, lev, 1e9) == pytest.approx(cap_stop)          # wider one is capped
    for side in (1, -1):
        pos = led.open('a', f'S{side + 1}USDT', side, 50, 100.0, T0, 10.0)
        liq = led.liquidation_price(pos)
        assert (pos.stop_price - liq) * side > 0                            # stop sits before liquidation
        assert led.net_pnl(pos, pos.stop_price) > -pos.initial_margin * 0.6  # loses ~50 % of margin, not all
    assert ledger(stop_loss_pct=0.0).stop_price(1, 100.0, 10, 1.0) == 0.0


def test_stop_loss_on_price_poll_and_on_candle_wick_and_restart(tmp_path):
    m = ScriptedMarket({'AUSDT': [101, 101, 101], 'BUSDT': [101, 101, 101]}, {'AUSDT': 2, 'BUSDT': 1})
    store = Store(tmp_path / 'f.db')
    e = Engine(settings(take_profit=0.0), m, store, [DIR])
    tick(e, m, 0)
    a = e.ledger.accounts['dir'].positions['AUSDT']
    b = e.ledger.accounts['dir'].positions['BUSDT']
    assert a.stop_price > 0 and b.stop_price > 0
    assert (a.stop_price - e.ledger.liquidation_price(a)) * a.side > 0
    reloaded = Ledger(e.settings, Store(tmp_path / 'f.db'), ['dir'], T0).accounts['dir'].positions
    assert reloaded['AUSDT'].stop_price == pytest.approx(a.stop_price)       # persisted across restarts
    m.live['AUSDT'] = a.stop_price - a.side * 1e-6
    m.offset = 10 * 60_000
    tick(e, m)
    assert 'AUSDT' not in e.ledger.accounts['dir'].positions
    m.live.clear()
    m.offset = 60_000
    tick(e, m, 1)
    stop_b = b.stop_price
    assert b.side == 1
    m.lows['BUSDT'] = e.ledger.liquidation_price(b) * 0.99                 # wick through stop AND liquidation
    tick(e, m, 2)
    trades = {t['symbol']: t for t in e.store.trades('dir')}
    assert trades['AUSDT']['reason'] == trades['BUSDT']['reason'] == 'stop_loss'
    assert trades['BUSDT']['exit_price'] == pytest.approx(stop_b) and trades['BUSDT']['proceeds'] > 0
    assert e.overview()['strategies'][0]['stop_losses'] == 2


def test_r_multiple_take_profit_balances_risk_and_reward():
    led = ledger(tp_r_multiple=1.5, stop_loss_pct=50.0)
    for side in (1, -1):
        pos = led.open('a', f'R{side + 1}USDT', side, 16, 100.0, T0, 1.5)          # stop 1.5 % away
        assert pos.stop_price == pytest.approx(pos.fill_price - side * 1.5)
        assert pos.tp_price == pytest.approx(pos.fill_price + side * 2.25)        # 1.5 R
        loss, win = led.net_pnl(pos, pos.stop_price), led.net_pnl(pos, pos.tp_price)
        assert loss == pytest.approx(-0.24 * pos.margin, abs=0.03 * pos.margin)   # ~ -24 % margin at 16x
        assert win > 1.2 * -loss                                                  # 1.5R gross, ~1.27R after fees
        assert led.is_target(pos, pos.tp_price) and not led.is_target(pos, pos.fill_price)
    assert ledger(tp_r_multiple=0.0).tp_price(1, 100.0, 98.0) == 0.0


def test_r_take_profit_on_price_poll_and_wick_and_defaults():
    m = ScriptedMarket({'AUSDT': [101, 101, 101], 'BUSDT': [101, 101, 101]}, {'AUSDT': 2, 'BUSDT': 1})
    e = Engine(settings(take_profit=0.0, tp_r_multiple=1.5), m, Store(':memory:'), [DIR])
    tick(e, m, 0)
    a = e.ledger.accounts['dir'].positions['AUSDT']
    b = e.ledger.accounts['dir'].positions['BUSDT']
    assert a.tp_price > a.fill_price > a.stop_price
    m.live['AUSDT'] = a.tp_price * 1.0001
    m.offset = 10 * 60_000
    tick(e, m)
    m.live.clear()
    m.offset = 60_000
    tick(e, m, 1)
    m.closes['BUSDT'][2] = b.fill_price                                         # close back near entry...
    tp_b = b.tp_price
    m.highs = {'BUSDT': tp_b * 1.001}                                           # ...but the wick hit the target
    tick(e, m, 2)
    trades = {t['symbol']: t for t in e.store.trades('dir')}
    assert trades['AUSDT']['reason'] == trades['BUSDT']['reason'] == 'take_profit'
    assert trades['BUSDT']['exit_price'] == pytest.approx(tp_b) and trades['BUSDT']['pnl'] > 0
    d = Settings()
    assert (d.take_profit, d.tp_r_multiple, d.risk_per_move, d.stop_loss_pct) == (0.0, 1.5, 0.25, 50.0)


def test_funding_settles_once_when_funding_time_passes():
    m = ScriptedMarket({'AUSDT': [101, 101]}, {'AUSDT': 1})
    e = Engine(settings(take_profit=0.0), m, Store(':memory:'), [DIR])
    funding_at = T0 + H + 30 * 60_000
    m.funding['AUSDT'] = (0.001, funding_at)
    tick(e, m, 0)                                   # position opened at T0+H+1min (before funding)
    pos = e.ledger.accounts['dir'].positions['AUSDT']
    m.offset = 40 * 60_000
    m.funding['AUSDT'] = (0.001, funding_at + 8 * H)
    tick(e, m)
    assert pos.funding == pytest.approx(pos.qty * 101 * 0.001)
    tick(e, m)
    assert pos.funding == pytest.approx(pos.qty * 101 * 0.001)   # not charged twice


def test_volume_rank_order_and_skips_when_margin_is_exhausted():
    closes = {f'{c}USDT': [101] for c in 'ABCDEF'}
    vol = {f'{c}USDT': v for c, v in zip('ABCDEF', [1, 6, 2, 5, 3, 4])}
    m = ScriptedMarket(closes, vol)
    e = Engine(settings(margin_pct=30.0), m, Store(':memory:'), [DIR])
    tick(e, m, 0)
    held = list(e.ledger.accounts['dir'].positions)
    assert held[:3] == ['BUSDT', 'DUSDT', 'FUSDT'] and len(held) >= 3
    assert [x['symbol'] for x in e.signals['dir']['skipped']] == [s for s in ['EUSDT', 'CUSDT', 'AUSDT'] if s not in held]


def test_restart_does_not_reprocess_candle(tmp_path):
    m = ScriptedMarket({'AUSDT': [101, 102]}, {'AUSDT': 1})
    store = Store(tmp_path / 'e.db')
    e = Engine(settings(), m, store, [DIR])
    tick(e, m, 0)
    store.close()
    store = Store(tmp_path / 'e.db')
    e2 = Engine(settings(), m, store, [DIR])
    tick(e2, m)
    assert len(store.positions()) == 1 and e2.last_candle == e.last_candle


# --- real strategies on the demo market -----------------------------------------------
def test_demo_market_is_deterministic_and_closed_only():
    s = settings(demo=True, top_n=5)
    m1, m2 = DemoMarket(s, start_ms=T0, clock=lambda: 0.0), DemoMarket(s, start_ms=T0, clock=lambda: 0.0)
    a, b = asyncio.run(m1.klines('ALFAUSDT', 300)), asyncio.run(m2.klines('ALFAUSDT', 300))
    pd.testing.assert_frame_equal(a, b)
    assert len(a) == 300 and int(a.index[-1]) + H <= m1.now_ms()
    q = asyncio.run(m1.quotes())['ALFAUSDT']
    assert q.price > 0 and q.next_funding > m1.now_ms() and abs(q.funding_rate) < 0.001


def test_all_strategies_trade_long_and_short_on_demo_market():
    s = settings(demo=True, top_n=8, demo_candle_seconds=10)
    clock = [0.0]
    e = Engine(s, DemoMarket(s, start_ms=T0, clock=lambda: clock[0]), Store(':memory:'))
    assert len(e.ledger.accounts) == len(STRATEGIES)
    for _ in range(80):
        clock[0] += 10.5
        asyncio.run(e.tick())
    ov = e.overview()
    assert sum(r['long_trades'] + r['open_long'] + r['short_trades'] + r['open_short'] for r in ov['strategies']) > 0
    for r in ov['strategies']:
        assert r['equity'] == pytest.approx(r['capital'] + r['realized'] + r['unrealized'])
    for key in e.by_key:
        for t in e.store.trades(key):
            assert 1 <= t['leverage'] <= 100 and isinstance(t['leverage'], int)
            if t['reason'] == 'take_profit':
                assert t['pnl'] >= 15 - 1e-9
    assert e.detail(STRATEGIES[0].key)['leverage_rule'].startswith('Leverage')


# --- API --------------------------------------------------------------------------------
def make_client(tmp_path, **kw):
    s = settings(demo=True, top_n=4, data_dir=tmp_path, **kw)
    clock = [0.0]
    return TestClient(create_app(s, start_worker=False, market=DemoMarket(s, start_ms=T0, clock=lambda: clock[0]))), clock


def test_api_endpoints_and_security_headers(tmp_path):
    client, clock = make_client(tmp_path)
    with client:
        clock[0] += 30
        asyncio.run(client.app.state.engine.tick())
        r = client.get('/api/overview')
        body = r.json()
        assert r.status_code == 200 and len(body['strategies']) == len(STRATEGIES) and body['status']['market'] == 'USDT-M futures'
        assert body['status']['take_profit'] == 15
        assert "script-src 'self'" in r.headers['content-security-policy'] and r.headers['cache-control'] == 'no-store'
        d = client.get('/api/strategy/consensus3').json()
        assert d['summary']['key'] == 'consensus3' and 'SHORT' in d['short_rule']
        assert client.get('/api/strategy/nope').status_code == 404
        assert set(client.get('/api/curves').json()) >= {s.key for s in STRATEGIES}
        assert client.get('/').status_code == 200 and client.get('/static/chart.umd.min.js').status_code == 200
        assert client.get('/static/../config.py').status_code == 404
        assert client.post('/api/overview').status_code == 405


def test_api_basic_auth(tmp_path):
    client, _ = make_client(tmp_path, user='admin', password='secret-pass')
    with client:
        assert client.get('/api/overview').status_code == 401
        assert client.get('/health').status_code == 200
        ok = base64.b64encode(b'admin:secret-pass').decode()
        bad = base64.b64encode(b'admin:wrong').decode()
        assert client.get('/api/overview', headers={'Authorization': 'Basic ' + ok}).status_code == 200
        assert client.get('/api/overview', headers={'Authorization': 'Basic ' + bad}).status_code == 401


# --- config -------------------------------------------------------------------------------
@pytest.mark.parametrize('kw', [
    dict(timeframe='3m'), dict(symbols=('btc',)), dict(top_n=0), dict(max_leverage=0), dict(max_leverage=200),
    dict(fee_rate=math.nan), dict(take_profit=-1), dict(user='a'), dict(binance_url='http://x.y'),
    dict(capital=50.0, margin_pct=1.0, min_margin=5.0), dict(max_leverage=125, maint_margin_rate=0.01),
])
def test_invalid_settings_rejected(kw):
    with pytest.raises(ValueError):
        Settings(**kw)


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('SYMBOLS', 'btcusdt, ETHUSDT,btcusdt')
    monkeypatch.setenv('TIMEFRAME', '4h')
    monkeypatch.setenv('TAKE_PROFIT_USDT', '15')
    monkeypatch.setenv('MAX_LEVERAGE', '10')
    s = Settings.load()
    assert s.symbols == ('BTCUSDT', 'ETHUSDT') and s.interval_ms == 4 * H and s.take_profit == 15 and s.max_leverage == 10
    assert 'fapi' in s.binance_url and s.db_path.name == 'futures.db'


# --- Binance futures client (mocked HTTP; only public GET endpoints) -----------------------
def test_binance_futures_parsing_and_rate_limit(monkeypatch):
    import httpx

    from app import market as market_mod
    from app.market import BinanceMarket

    now = T0 + 30 * 60_000
    calls = {'429': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == 'GET' and request.url.path.startswith('/fapi/v1/')
        path = request.url.path
        if path.endswith('/exchangeInfo'):
            mk = lambda s, b, **kw: {'symbol': s, 'baseAsset': b, 'quoteAsset': 'USDT', 'status': 'TRADING',  # noqa: E731
                                     'contractType': 'PERPETUAL', 'underlyingType': 'COIN', **kw}
            return httpx.Response(200, json={'symbols': [
                mk('BTCUSDT', 'BTC'), mk('ETHUSDT', 'ETH'), mk('USDCUSDT', 'USDC'), mk('OLDUSDT', 'OLD', status='SETTLING'),
                mk('BTCUSDT_261225', 'BTC', contractType='CURRENT_QUARTER'), mk('XAUUSDT', 'XAU', underlyingType='COMMODITY')]})
        if path.endswith('/ticker/24hr'):
            if not calls['429']:
                calls['429'] += 1
                return httpx.Response(429, headers={'Retry-After': '0'})
            return httpx.Response(200, json=[{'symbol': 'BTCUSDT', 'quoteVolume': '5'}, {'symbol': 'ETHUSDT', 'quoteVolume': '9'},
                                             {'symbol': 'USDCUSDT', 'quoteVolume': '99'}, {'symbol': 'XAUUSDT', 'quoteVolume': '50'}])
        if path.endswith('/premiumIndex'):
            return httpx.Response(200, json=[
                {'symbol': 'BTCUSDT', 'markPrice': '100.5', 'lastFundingRate': '0.0001', 'nextFundingTime': T0 + 8 * H},
                {'symbol': 'BAD', 'markPrice': 'x'}])
        if path.endswith('/klines'):
            return httpx.Response(200, json=[[T0 - H, '1', '2', '0.5', '1.5', '10'], [T0, '1.5', '2', '1', '1.8', '11']])
        return httpx.Response(404)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            m = BinanceMarket(settings(top_n=5), client)
            monkeypatch.setattr(m, 'now_ms', lambda: now)
            return await m.universe(), await m.quotes(), await m.klines('BTCUSDT', 10)

    real_sleep = asyncio.sleep
    monkeypatch.setattr(market_mod.asyncio, 'sleep', lambda s: real_sleep(0))
    uni, quotes, k = asyncio.run(go())
    assert uni == [('ETHUSDT', 9.0), ('BTCUSDT', 5.0)]          # stablecoin, quarterly, commodity, halted removed
    assert quotes == {'BTCUSDT': Quote(100.5, 0.0001, T0 + 8 * H)}
    assert list(k.index) == [T0 - H]                            # forming candle dropped


# --- pause new entries ----------------------------------------------------------------
def test_pause_blocks_new_entries_but_open_positions_still_exit(tmp_path):
    m = ScriptedMarket({'AUSDT': [101, 101, 101, 101], 'BUSDT': [96, 96, 101, 101]}, {'AUSDT': 2, 'BUSDT': 1})
    e = Engine(settings(take_profit=0.0), m, Store(tmp_path / 'p.db'), [DIR])
    tick(e, m, 0)
    a = e.ledger.accounts['dir'].positions['AUSDT']               # opened before the pause
    assert 'BUSDT' not in e.ledger.accounts['dir'].positions
    res = e.set_entries_paused(True)
    assert res['entries_paused'] and res['open_positions'] == 1
    tick(e, m, 1)
    tick(e, m, 2)                                                  # fresh BUSDT long signal -> not opened
    assert 'BUSDT' not in e.ledger.accounts['dir'].positions
    assert e.detail('dir')['signals']['paused'] == [{'symbol': 'BUSDT', 'side': 'LONG'}]
    assert e.status()['entries_paused'] is True
    m.live['AUSDT'] = a.stop_price - 1e-6                          # the open one still hits its stop
    m.offset = 10 * 60_000
    tick(e, m)
    assert 'AUSDT' not in e.ledger.accounts['dir'].positions
    assert e.store.trades('dir')[0]['reason'] == 'stop_loss'
    e2 = Engine(settings(take_profit=0.0), m, Store(tmp_path / 'p.db'), [DIR])
    assert e2.entries_paused and e2.paused_at                      # survives a restart
    e2.set_entries_paused(False)
    assert not Engine(settings(take_profit=0.0), m, Store(tmp_path / 'p.db'), [DIR]).entries_paused


def test_pause_endpoint_toggles_and_rejects_foreign_or_bare_requests(tmp_path):
    client, clock = make_client(tmp_path)
    ok = {'X-Dashboard': '1', 'Origin': 'http://testserver'}
    with client:
        assert client.get('/api/overview').json()['status']['entries_paused'] is False
        r = client.post('/api/control/entries', json={'paused': True}, headers=ok)
        assert r.status_code == 200 and r.json()['entries_paused'] is True
        assert client.get('/api/overview').json()['status']['entries_paused'] is True
        assert client.post('/api/control/entries', json={'paused': True}).status_code == 403          # no header
        assert client.post('/api/control/entries', json={'paused': False},
                           headers={'X-Dashboard': '1', 'Origin': 'https://evil.example'}).status_code == 403
        assert client.post('/api/control/entries', json={'paused': 'no'}, headers=ok).status_code == 422
        assert client.post('/api/overview', headers=ok).status_code == 405                        # still read-only
        assert client.post('/api/control/entries', json={'paused': False}, headers=ok).json()['entries_paused'] is False


def test_pause_endpoint_requires_basic_auth_when_configured(tmp_path):
    client, _ = make_client(tmp_path, user='u', password='p')
    with client:
        h = {'X-Dashboard': '1'}
        assert client.post('/api/control/entries', json={'paused': True}, headers=h).status_code == 401
        assert client.post('/api/control/entries', json={'paused': True}, headers=h, auth=('u', 'p')).status_code == 200


def test_export_zip_has_every_strategy(tmp_path, monkeypatch):
    import zipfile
    from app import export
    m = ScriptedMarket({'AUSDT': [101, 101, 101]}, {'AUSDT': 1})
    e = Engine(settings(take_profit=0.0), m, Store(tmp_path / 'f.db'), [DIR])
    tick(e, m, 0)
    monkeypatch.setenv('DB', str(tmp_path / 'f.db'))
    monkeypatch.setenv('HOME', str(tmp_path))
    with zipfile.ZipFile(export.main()) as z:
        names = z.namelist()
        assert 'futures.db' in names and 'XULASE.csv' in names
        assert 'strategiyalar/dir/aciq_movqeler.csv' in names
        assert 'AUSDT' in z.read('strategiyalar/dir/aciq_movqeler.csv').decode('utf-8-sig')


# --- cost filter, selective signal exits, cooldown, max hold ---------------------------
def test_cost_filter_and_signal_exit_classification():
    from app.futures import OWN_EXIT_RULES, cost_r, exits_on_signal
    assert cost_r('adx', 0.003, 0.001) == pytest.approx(2 * 0.001 / 0.009)            # BTC-like: 22 % of R
    assert cost_r('adx', 0.02, 0.001) == pytest.approx(2 * 0.001 / 0.06)              # alt: 3 % of R
    assert cost_r('williams', float('nan'), 0.001) == math.inf
    assert exits_on_signal('williams', 'auto') and not exits_on_signal('adx', 'auto')
    assert exits_on_signal('adx', 'all') and not exits_on_signal('williams', 'none')
    from backtest.strategies import MEMBERS
    assert OWN_EXIT_RULES <= {s.key for s in MEMBERS}


def _calm(settings_kw, closes=None):
    m = ScriptedMarket({'AUSDT': closes or [101, 101, 101, 101, 101]}, {'AUSDT': 1})
    return m, Engine(settings(take_profit=0.0, **settings_kw), m, Store(':memory:'), [DIR])


def test_fee_filter_skips_entries_where_costs_eat_the_risk():
    m, e = _calm(dict(max_cost_r=0.10))                 # scripted bars: ATR ~0.1 % -> costs >> 10 % of R
    tick(e, m, 0)
    assert not e.ledger.accounts['dir'].positions
    f = e.detail('dir')['signals']['filtered']
    assert f and f[0]['why'] == 'fee' and f[0]['cost_r'] > 0.10
    m, e = _calm(dict(max_cost_r=0.0))
    tick(e, m, 0)
    assert 'AUSDT' in e.ledger.accounts['dir'].positions


def test_stop_tp_only_rules_ignore_opposite_signal_and_time_out():
    m, e = _calm(dict(signal_exits='none', max_hold_hours=2.0), [101, 90, 90, 90, 90])
    tick(e, m, 0)
    pos = e.ledger.accounts['dir'].positions['AUSDT']
    m.live['AUSDT'] = pos.fill_price                   # keep the mark flat: no stop / TP
    tick(e, m, 1)                                       # opposite (short) signal: ignored, no reverse
    assert e.ledger.accounts['dir'].positions['AUSDT'].side == 1 and not e.store.trades('dir')
    m.bar, m.offset = 2, 60_000
    tick(e, m)                                          # > 2 h held -> timeout
    assert e.store.trades('dir')[0]['reason'] == 'timeout'


def test_cooldown_blocks_reentry_on_the_same_coin():
    m, e = _calm(dict(cooldown_bars=3), [101, 90, 101, 101, 101, 101])
    tick(e, m, 0)
    tick(e, m, 1)                                       # long closed by the short signal; short re-entry blocked
    assert e.store.trades('dir')[0]['reason'] in ('reverse', 'liquidation')     # 11 % gap through the stop
    assert 'AUSDT' not in e.ledger.accounts['dir'].positions
    assert e.detail('dir')['signals']['filtered'][0]['why'] == 'cooldown'
    tick(e, m, 2)                                       # fresh long 1 candle later: still cooling down
    assert 'AUSDT' not in e.ledger.accounts['dir'].positions
    e2 = Engine(e.settings, m, e.store, [DIR])          # cooldown survives a restart (rebuilt from trades)
    assert ('dir', 'AUSDT') in e2.last_close


def test_new_defaults():
    d = Settings()
    assert (d.max_cost_r, d.signal_exits, d.cooldown_bars, d.max_hold_hours) == (0.10, 'all', 0, 0.0)
    with pytest.raises(ValueError):
        Settings(signal_exits='sometimes')


# --- one consensus account -----------------------------------------------------------
def _above(level):
    def fn(df):
        if df.close.iloc[-1] > 1:
            return (df.close > level).astype(float).to_numpy()
        return ((1 / df.close) < 90).astype(float).to_numpy()
    return fn


VOTERS = [Strategy(k, k, 'Test', '', _above(lv)) for k, lv in (('a', 100), ('b', 100), ('c', 102), ('d', 200), ('e', 200))]


def test_one_account_opens_one_order_when_three_of_five_agree(monkeypatch):
    from backtest import strategies as rules
    monkeypatch.setattr(rules, 'MEMBERS', VOTERS)
    m = ScriptedMarket({'AUSDT': [101, 101, 103, 103]}, {'AUSDT': 1})
    e = Engine(settings(take_profit=0.0), m, Store(':memory:'), [rules.CONSENSUS])
    assert list(e.ledger.accounts) == ['consensus3']
    acc = e.ledger.accounts['consensus3']
    tick(e, m, 0)                                       # a, b long -> 2/5: no order
    assert not acc.positions
    assert e.detail('consensus3')['signals']['votes'][0] == {'symbol': 'AUSDT', 'side': 'LONG', 'members': ['a', 'b']}
    tick(e, m, 1)
    assert not acc.positions
    tick(e, m, 2)                                       # c joins -> 3/5: one order in the single account
    assert list(acc.positions) == ['AUSDT'] and acc.positions['AUSDT'].side == 1
    assert e.detail('consensus3')['signals']['votes'][0]['members'] == ['a', 'b', 'c']
    tick(e, m, 3)                                       # still 3/5: no second order
    assert len(acc.positions) == 1 and not e.store.trades('consensus3')
    assert e.status()['consensus_min'] == 3 and len(e.status()['voters']) == 5


def test_branch_has_top5_plus_consensus_accounts(tmp_path):
    from backtest import strategies as rules
    assert [s.key for s in STRATEGIES] == ['ema_pullback', 'williams', 'adx', 'rr_2to1', 'confluence', 'consensus3']
    e = Engine(settings(), ScriptedMarket({'AUSDT': [101]}, {'AUSDT': 1}), Store(tmp_path / 'six.db'))
    assert len(e.ledger.accounts) == 6
    assert [m.key for m in rules.MEMBERS] == ['ema_pullback', 'williams', 'adx', 'rr_2to1', 'confluence']


def test_members_trade_alone_and_consensus_waits_for_three(monkeypatch):
    from backtest import strategies as rules
    monkeypatch.setattr(rules, 'MEMBERS', VOTERS)
    m = ScriptedMarket({'AUSDT': [101, 101, 103]}, {'AUSDT': 1})
    e = Engine(settings(take_profit=0.0), m, Store(':memory:'), [*VOTERS, rules.CONSENSUS])
    held = lambda: {k for k, acc in e.ledger.accounts.items() if 'AUSDT' in acc.positions}  # noqa: E731
    tick(e, m, 0)
    assert held() == {'a', 'b'}                          # members open their own orders at once
    tick(e, m, 2)
    assert held() == {'a', 'b', 'c', 'consensus3'}       # c joins -> 3/5 -> the consensus account opens too


def test_consensus_order_remembers_which_three_agreed(monkeypatch, tmp_path):
    import sqlite3
    import zipfile
    from app import export
    from backtest import strategies as rules
    monkeypatch.setattr(rules, 'MEMBERS', VOTERS)
    db = tmp_path / 'v.db'
    m = ScriptedMarket({'AUSDT': [101, 101, 103, 103]}, {'AUSDT': 1})
    e = Engine(settings(take_profit=0.0), m, Store(db), [*VOTERS, rules.CONSENSUS])
    tick(e, m, 0)
    tick(e, m, 2)
    pos = e.ledger.accounts['consensus3'].positions['AUSDT']
    assert pos.voters == 'a,b,c'
    assert e.ledger.accounts['a'].positions['AUSDT'].voters == ''           # single-rule accounts: empty
    assert e.detail('consensus3')['positions'][0]['voters'] == 'a,b,c'
    assert e.detail('consensus3')['signals']['entries'][0]['voters'] == 'a,b,c'
    reloaded = Ledger(e.settings, Store(db), list(e.by_key), T0).accounts['consensus3'].positions['AUSDT']
    assert reloaded.voters == 'a,b,c'                                       # survives a restart
    m.live['AUSDT'] = pos.stop_price - 1e-6
    m.offset = 10 * 60_000
    tick(e, m)
    t = e.store.trades('consensus3')[0]
    assert t['reason'] == 'stop_loss' and t['voters'] == 'a,b,c'
    monkeypatch.setenv('DB', str(db))
    monkeypatch.setenv('HOME', str(tmp_path))
    with zipfile.ZipFile(export.main()) as z:
        csv_text = z.read('strategiyalar/consensus3/emeliyyatlar.csv').decode('utf-8-sig')
    assert 'razilasanlar' in csv_text and 'a + b + c' in csv_text
    old = tmp_path / 'old.db'                                               # a database from before this column
    con = sqlite3.connect(old)
    con.executescript('CREATE TABLE positions (key TEXT, symbol TEXT, side INTEGER, leverage INTEGER, qty REAL, '
                      'entry_price REAL, fill_price REAL, margin REAL, fee REAL, funding REAL, opened_at INTEGER, '
                      'PRIMARY KEY (key, symbol));'
                      'CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, symbol TEXT, side INTEGER, '
                      'leverage INTEGER, qty REAL, entry_price REAL, exit_price REAL, margin REAL, notional REAL, proceeds REAL, '
                      'pnl REAL, ret REAL, fees REAL, funding REAL, opened_at INTEGER, closed_at INTEGER, reason TEXT);')
    con.close()
    s = Store(old)
    for table in ('positions', 'trades'):
        assert 'voters' in {r['name'] for r in s.db.execute(f'PRAGMA table_info({table})')}
