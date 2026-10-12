"""Live 20-account paper engine: ledger accounting, signal execution, persistence, API."""
from __future__ import annotations

import asyncio
import base64

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.demo import DemoMarket
from app.engine import Engine, evaluate
from app.ledger import Ledger
from app.main import create_app
from app.store import Store
from backtest.strategies import STRATEGIES, Strategy

H = 3_600_000
T0 = 1_790_000_000_000 // H * H


def settings(**kw) -> Settings:
    base = dict(capital=2000.0, max_positions=4, fee_rate=0.001, slippage_rate=0.0005, min_order=10.0, candle_delay_seconds=0)
    return Settings(**{**base, **kw})


# --- ledger ------------------------------------------------------------------------
def test_buy_sell_accounting_and_equity_identity():
    s = settings()
    store = Store(':memory:')
    led = Ledger(s, store, ['a'], T0)
    pos = led.buy('a', 'XUSDT', 100.0, T0)
    assert pos.cost == pytest.approx(500.0)              # equity / max_positions
    assert pos.qty == pytest.approx((500 - 0.5) / (100 * 1.0005))
    assert led.accounts['a'].cash == pytest.approx(1500.0)
    eq = led.update_prices({'XUSDT': 110.0})['a']
    liq = pos.qty * 110 * 0.9995 * 0.999
    assert eq == pytest.approx(1500 + liq)
    trade = led.sell('a', 'XUSDT', 110.0, T0 + H, 'signal')
    assert trade['proceeds'] == pytest.approx(liq)
    assert trade['pnl'] == pytest.approx(liq - 500)
    assert trade['fees'] == pytest.approx(0.5 + pos.qty * 110 * 0.9995 * 0.001)
    acc = led.accounts['a']
    assert acc.cash == pytest.approx(2000 + trade['pnl']) and not acc.positions
    # capital + realized + unrealized == equity, always
    led.buy('a', 'YUSDT', 50.0, T0 + H)
    led.update_prices({'YUSDT': 45.0})
    unreal = sum(led.liquidation_value(p) - p.cost for p in acc.positions.values())
    assert led.equity('a') == pytest.approx(2000 + trade['pnl'] + unreal)


def test_round_trip_at_same_price_costs_fees_and_slippage():
    led = Ledger(settings(), Store(':memory:'), ['a'], T0)
    led.buy('a', 'XUSDT', 10.0, T0)
    t = led.sell('a', 'XUSDT', 10.0, T0, 'signal')
    assert t['ret'] == pytest.approx((1 - 0.001) ** 2 * 0.9995 / 1.0005 - 1)
    assert t['pnl'] < 0


def test_slots_min_order_and_no_double_buy():
    led = Ledger(settings(max_positions=2), Store(':memory:'), ['a'], T0)
    assert led.buy('a', 'AUSDT', 1.0, T0)
    assert led.buy('a', 'AUSDT', 1.0, T0) is None        # already held
    assert led.buy('a', 'BUSDT', 1.0, T0)
    assert led.buy('a', 'CUSDT', 1.0, T0) is None        # slots full
    led2 = Ledger(settings(max_positions=4, capital=40.0), Store(':memory:'), ['b'], T0)
    for sym in ('AUSDT', 'BUSDT', 'CUSDT', 'DUSDT'):
        led2.buy('b', sym, 1.0, T0)
    led2.update_prices({'AUSDT': 0.1, 'BUSDT': 0.1, 'CUSDT': 0.1})
    led2.sell('b', 'DUSDT', 1.0, T0, 'signal')
    assert led2.order_size('b') == 0.0                   # below MIN_ORDER_USDT


def test_ledger_persists_across_restart(tmp_path):
    s = settings()
    store = Store(tmp_path / 'x.db')
    led = Ledger(s, store, ['a', 'b'], T0)
    led.buy('a', 'XUSDT', 100.0, T0)
    led.buy('b', 'XUSDT', 100.0, T0)
    led.sell('b', 'XUSDT', 120.0, T0 + H, 'signal')
    store.close()
    store = Store(tmp_path / 'x.db')
    led2 = Ledger(s, store, ['a', 'b'], T0 + 5 * H)
    assert set(led2.accounts['a'].positions) == {'XUSDT'}
    assert led2.accounts['a'].cash == pytest.approx(1500)
    assert led2.accounts['b'].cash > 2000 and not led2.accounts['b'].positions
    assert led2.accounts['a'].started_at == T0
    assert len(store.trades('b')) == 1


# --- engine with a scripted market ---------------------------------------------------
class ScriptedMarket:
    """Each symbol has a scripted close series; strategy 'flag' holds when close > 100."""
    demo = False

    def __init__(self, closes: dict[str, list[float]], volume: dict[str, float]):
        self.closes, self.volume = closes, volume
        self.bar = 0               # index of the bar that just closed
        self.stale: set[str] = set()

    def now_ms(self):
        return T0 + (self.bar + 1) * H + 60_000

    async def universe(self):
        return sorted(self.volume.items(), key=lambda x: -x[1])

    async def prices(self):
        return {s: c[self.bar] for s, c in self.closes.items()}

    async def klines(self, symbol, limit):
        c = np.array(self.closes[symbol][:self.bar + 1], dtype=float)
        hist = np.full(100, 90.0)
        close = np.concatenate([hist, c])
        idx = T0 + np.arange(-len(hist), len(c)) * H     # last bar == the candle that just closed
        df = pd.DataFrame({'open': close, 'high': close, 'low': close, 'close': close, 'volume': 1.0},
                          index=pd.Index(idx.astype('int64'), name='time'))
        if symbol in self.stale:
            df = df.iloc[:-1]
        return df.tail(limit)


FLAG = Strategy('flag', 'Flag', 'Test', 'close > 100', lambda df: (df.close > 100).astype(float).to_numpy())
INVERSE = Strategy('inverse', 'Inverse', 'Test', 'close < 95', lambda df: (df.close < 95).astype(float).to_numpy())


def run(engine, market, bar):
    market.bar = bar
    asyncio.run(engine.tick())


def test_engine_enters_on_fresh_signal_and_exits_on_state_zero():
    m = ScriptedMarket({'AUSDT': [101, 102, 103, 99, 105], 'BUSDT': [90, 101, 102, 103, 104]}, {'AUSDT': 2, 'BUSDT': 1})
    e = Engine(settings(), m, Store(':memory:'), [FLAG, INVERSE])
    run(e, m, 0)
    # A already above 100 on the very first bar? 90 -> 101 is a fresh 0->1 transition, so it is bought.
    assert set(e.ledger.accounts['flag'].positions) == {'AUSDT'}
    run(e, m, 1)
    assert set(e.ledger.accounts['flag'].positions) == {'AUSDT', 'BUSDT'}
    assert e.signals['flag']['entries'] == ['BUSDT']
    run(e, m, 2)                                          # still held, no re-entry
    assert e.store.trades('flag') == []
    run(e, m, 3)                                          # A closes at 99 -> exit
    assert set(e.ledger.accounts['flag'].positions) == {'BUSDT'}
    assert e.signals['flag']['exits'] == ['AUSDT']
    t = e.store.trades('flag')[0]
    assert t['symbol'] == 'AUSDT' and t['entry_price'] == 101 and t['exit_price'] == 99
    run(e, m, 4)                                          # A back above -> fresh entry again
    assert set(e.ledger.accounts['flag'].positions) == {'AUSDT', 'BUSDT'}
    assert e.ledger.accounts['inverse'].positions == {}   # independent account untouched
    assert e.ledger.accounts['inverse'].cash == 2000


def test_no_entry_when_regime_already_on_and_same_candle_processed_once():
    m = ScriptedMarket({'AUSDT': [101, 102]}, {'AUSDT': 1})
    m.closes['AUSDT'] = [101, 102]
    e = Engine(settings(), m, Store(':memory:'), [FLAG])
    m.bar = 1                                             # first seen bar: state was already 1 on bar 0
    asyncio.run(e.tick())
    assert e.ledger.accounts['flag'].positions == {}
    assert e.signals['flag']['active'] == ['AUSDT']
    n = len(e.events)
    asyncio.run(e.tick())                                 # same candle again: nothing happens
    assert len(e.events) == n


def test_slot_priority_follows_volume_rank_and_records_skips():
    closes = {f'{c}USDT': [101] for c in 'ABCDEF'}
    vol = {f'{c}USDT': v for c, v in zip('ABCDEF', [1, 6, 2, 5, 3, 4])}
    m = ScriptedMarket(closes, vol)
    e = Engine(settings(max_positions=4), m, Store(':memory:'), [FLAG])
    run(e, m, 0)
    assert set(e.ledger.accounts['flag'].positions) == {'BUSDT', 'DUSDT', 'FUSDT', 'EUSDT'}
    assert e.signals['flag']['skipped'] == ['CUSDT', 'AUSDT']


def test_stale_candles_are_ignored():
    m = ScriptedMarket({'AUSDT': [101], 'BUSDT': [101]}, {'AUSDT': 2, 'BUSDT': 1})
    m.stale = {'BUSDT'}
    e = Engine(settings(), m, Store(':memory:'), [FLAG])
    run(e, m, 0)
    assert set(e.ledger.accounts['flag'].positions) == {'AUSDT'}


def test_engine_restart_does_not_reprocess_candle(tmp_path):
    m = ScriptedMarket({'AUSDT': [101, 102]}, {'AUSDT': 1})
    store = Store(tmp_path / 'e.db')
    e = Engine(settings(), m, store, [FLAG])
    run(e, m, 0)
    store.close()
    store = Store(tmp_path / 'e.db')
    e2 = Engine(settings(), m, store, [FLAG])
    assert e2.last_candle == e.last_candle
    asyncio.run(e2.tick())
    assert len(store.positions()) == 1 and e2.ledger.accounts['flag'].cash == pytest.approx(1500)
    overview = e2.overview()
    assert overview['strategies'][0]['open_positions'] == 1


# --- real strategies on the demo market ---------------------------------------------
def test_demo_market_is_deterministic_and_closed_only():
    s = settings(demo=True, top_n=5)
    clock = [0.0]
    m1, m2 = DemoMarket(s, start_ms=T0, clock=lambda: clock[0]), DemoMarket(s, start_ms=T0, clock=lambda: clock[0])
    a = asyncio.run(m1.klines('ALFAUSDT', 300))
    b = asyncio.run(m2.klines('ALFAUSDT', 300))
    pd.testing.assert_frame_equal(a, b)
    assert len(a) == 300 and int(a.index[-1]) + H <= m1.now_ms()
    assert (a.high >= a[['open', 'close']].max(axis=1)).all() and (a.low <= a[['open', 'close']].min(axis=1)).all()


def test_all_20_strategies_trade_independently_on_demo_market():
    s = settings(demo=True, top_n=12, max_positions=10, demo_candle_seconds=10)
    clock = [0.0]
    m = DemoMarket(s, start_ms=T0, clock=lambda: clock[0])
    e = Engine(s, m, Store(':memory:'))
    assert len(e.ledger.accounts) == 20
    for _ in range(40):
        clock[0] += 10.5
        asyncio.run(e.tick())
    ov = e.overview()
    assert len(ov['strategies']) == 20
    assert sum(r['trades'] + r['open_positions'] for r in ov['strategies']) > 0
    for r in ov['strategies']:
        assert r['equity'] == pytest.approx(r['capital'] + r['realized'] + r['unrealized'])
        assert r['open_positions'] <= 10
    d = e.detail(STRATEGIES[0].key)
    assert d['summary']['key'] == STRATEGIES[0].key and d['equity']


def test_evaluate_isolates_a_failing_strategy():
    boom = Strategy('boom', 'Boom', 'Test', '', lambda df: 1 / 0)
    df = pd.DataFrame({'open': [1.0] * 70, 'high': 1.0, 'low': 1.0, 'close': [99.0] * 69 + [101.0], 'volume': 1.0})
    out = evaluate([boom, FLAG], {'AUSDT': df})
    assert out['boom'] == {} and out['flag']['AUSDT'] == (0, 1)


# --- API ------------------------------------------------------------------------------
def make_client(tmp_path, **kw):
    s = settings(demo=True, top_n=6, data_dir=tmp_path, **kw)
    clock = [0.0]
    market = DemoMarket(s, start_ms=T0, clock=lambda: clock[0])
    return TestClient(create_app(s, start_worker=False, market=market)), clock


def test_api_endpoints_and_security_headers(tmp_path):
    client, clock = make_client(tmp_path)
    with client:
        engine = client.app.state.engine
        clock[0] += 30
        asyncio.run(engine.tick())
        r = client.get('/api/overview')
        assert r.status_code == 200
        body = r.json()
        assert len(body['strategies']) == 20 and body['status']['mode'] == 'demo'
        assert "script-src 'self'" in r.headers['content-security-policy']
        assert r.headers['cache-control'] == 'no-store'
        assert client.get('/api/strategy/rsi').json()['summary']['key'] == 'rsi'
        assert client.get('/api/strategy/nope').status_code == 404
        assert set(client.get('/api/curves').json()) >= {s.key for s in STRATEGIES}
        assert client.get('/').status_code == 200
        assert client.get('/static/chart.umd.min.js').status_code == 200
        assert client.get('/static/../config.py').status_code == 404
        assert client.post('/api/overview').status_code == 405


def test_api_basic_auth(tmp_path):
    client, _ = make_client(tmp_path, user='admin', password='secret-pass')
    with client:
        assert client.get('/api/overview').status_code == 401
        assert client.get('/health').status_code == 200
        token = base64.b64encode(b'admin:secret-pass').decode()
        assert client.get('/api/overview', headers={'Authorization': 'Basic ' + token}).status_code == 200
        bad = base64.b64encode(b'admin:wrong').decode()
        assert client.get('/api/overview', headers={'Authorization': 'Basic ' + bad}).status_code == 401


# --- config -----------------------------------------------------------------------------
@pytest.mark.parametrize('kw', [
    dict(timeframe='3m'), dict(symbols=('btc',)), dict(top_n=0), dict(max_positions=0),
    dict(fee_rate=float('nan')), dict(user='a'), dict(binance_url='http://x.y'),
    dict(capital=50.0, min_order=10.0, max_positions=10),
])
def test_invalid_settings_rejected(kw):
    with pytest.raises(ValueError):
        Settings(**kw)


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('SYMBOLS', 'btcusdt, ETHUSDT,btcusdt')
    monkeypatch.setenv('TIMEFRAME', '4h')
    monkeypatch.setenv('START_CAPITAL', '2000')
    s = Settings.load()
    assert s.symbols == ('BTCUSDT', 'ETHUSDT') and s.interval_ms == 4 * H and s.capital == 2000


# --- Binance client (mocked HTTP; only public GET endpoints) ----------------------------
def test_binance_market_parsing_and_rate_limit(monkeypatch):
    import httpx

    from app import market as market_mod
    from app.market import BinanceMarket

    now = T0 + 30 * 60_000
    calls = {'429': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == 'GET'
        path = request.url.path
        if path.endswith('/exchangeInfo'):
            mk = lambda s, b, **kw: {'symbol': s, 'baseAsset': b, 'quoteAsset': 'USDT', 'status': 'TRADING', **kw}  # noqa: E731
            return httpx.Response(200, json={'symbols': [mk('BTCUSDT', 'BTC'), mk('ETHUSDT', 'ETH'), mk('USDCUSDT', 'USDC'),
                                                          mk('BTCUPUSDT', 'BTCUP'), mk('OLDUSDT', 'OLD', status='BREAK')]})
        if path.endswith('/ticker/24hr'):
            if not calls['429']:
                calls['429'] += 1
                return httpx.Response(429, headers={'Retry-After': '0'})
            return httpx.Response(200, json=[{'symbol': 'BTCUSDT', 'quoteVolume': '5'}, {'symbol': 'ETHUSDT', 'quoteVolume': '9'},
                                             {'symbol': 'USDCUSDT', 'quoteVolume': '99'}])
        if path.endswith('/ticker/price'):
            return httpx.Response(200, json=[{'symbol': 'BTCUSDT', 'price': '100.5'}, {'symbol': 'BAD', 'price': 'x'}])
        if path.endswith('/klines'):
            rows = [[T0 - H, '1', '2', '0.5', '1.5', '10'], [T0, '1.5', '2', '1', '1.8', '11']]  # 2nd still forming
            return httpx.Response(200, json=rows)
        return httpx.Response(404)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            m = BinanceMarket(settings(top_n=5), client)
            monkeypatch.setattr(m, 'now_ms', lambda: now)
            uni = await m.universe()
            prices = await m.prices()
            k = await m.klines('BTCUSDT', 10)
            return uni, prices, k

    real_sleep = asyncio.sleep
    monkeypatch.setattr(market_mod.asyncio, 'sleep', lambda s: real_sleep(0))
    uni, prices, k = asyncio.run(go())
    assert uni == [('ETHUSDT', 9.0), ('BTCUSDT', 5.0)]       # stablecoins / leveraged / halted removed
    assert prices == {'BTCUSDT': 100.5}
    assert list(k.index) == [T0 - H] and k.close.iloc[-1] == 1.5
