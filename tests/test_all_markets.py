import asyncio

import httpx
import pytest

from app.config import Settings
from app.engine import Engine
from app.market import Market, MarketError, demo_snapshot
from app.paper import build_config
from app.store import Store


def test_discovery_filters_and_has_no_count_limit():
    async def run():
        base = dict(status='TRADING', contractType='PERPETUAL', quoteAsset='USDT', marginAsset='USDT')
        items = [dict(base, symbol=f'COIN{i}USDT') for i in range(501)]
        items += [dict(base, symbol='BTCUSDT', status='SETTLING'),
                  dict(base, symbol='ETHUSDT', contractType='CURRENT_QUARTER'),
                  dict(base, symbol='SOLUSDC', quoteAsset='USDC')]
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={'symbols': items}))) as client:
            symbols = await Market(client).discover_symbols()
        assert len(symbols) == 501
        Settings(symbols=symbols)
    asyncio.run(run())


def test_all_config_uses_dynamic_pair_pattern():
    config = build_config(Settings(symbols=('ALL',)), 'x'*32, 'user', 'password')
    assert config['exchange']['pair_whitelist'] == ['.*/USDT:USDT']
    assert config['max_open_trades'] == -1


def test_discovery_refresh_and_recovery(tmp_path):
    async def run():
        store = Store(tmp_path/'markets.db')
        async with httpx.AsyncClient() as client:
            engine = Engine(Settings(symbols=('ALL',)), client, store)
            async def discover(): return ('BTCUSDT', 'ETHUSDT')
            async def snapshot(symbol): return demo_snapshot(symbol)
            engine.market.discover_symbols = discover
            engine.market.snapshot = snapshot
            await engine.scan()
            assert engine.status()['market_count'] == 2
            assert engine.status()['scan_completed'] == 2
            async def failure(): raise MarketError('unavailable')
            engine.market.discover_symbols = failure
            await engine.scan()
            assert engine.status()['discovery_error']
            assert len(engine.rows) == 2
            async def changed(): return ('SOLUSDT',)
            engine.market.discover_symbols = changed
            await engine.scan()
            assert set(engine.rows) == {'SOLUSDT'}
            assert engine.status()['discovery_error'] is None
        store.close()
    asyncio.run(run())
