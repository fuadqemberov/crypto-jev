from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_market_view_validates_symbols_caches_and_returns_public_data(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, symbols=('BTCUSDT',)), start_worker=False)
    with TestClient(app) as client:
        calls = []
        async def market_get(path, **params):
            calls.append((path, params))
            if path.endswith('klines'):
                return [[123, '100', '102', '99', '101', '5']]
            if path.endswith('depth'):
                return {'bids': [['100', '1']], 'asks': [['101', '2']]}
            return {'markPrice': '100.5', 'lastFundingRate': '.0001'}
        app.state.engine.market.get = market_get
        assert client.get('/api/market/INVALID').status_code == 404
        assert client.get('/api/market/BTCUSDT?interval=bad').status_code == 404
        assert calls == []
        data = client.get('/api/market/BTCUSDT?interval=1h').json()
        assert data['candles'][0]['open'] == 100
        assert data['mark'] == 100.5 and data['interval'] == '1h'
        assert data['bids'] == [['100', '1']]
        assert len(calls) == 3
        assert client.get('/api/market/BTCUSDT?interval=1h').json() == data
        assert len(calls) == 3


def test_market_view_failure_does_not_expose_upstream_errors(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, symbols=('BTCUSDT',)), start_worker=False)
    with TestClient(app) as client:
        async def fail(*args, **kwargs):
            raise ValueError('private upstream details')
        app.state.engine.market.get = fail
        response = client.get('/api/market/BTCUSDT')
        assert response.status_code == 503
        assert 'private' not in response.text
