"""A directional analysis is not an executable entry until all bridge gates pass."""

import pytest

from app.config import Settings
from app.engine import Engine
from app.store import Store
from test_execution import connected, row


@pytest.mark.parametrize('gate,ready,code', [
    ('accepted', True, None),
    ('risk_error', False, 'risk_error'),
    ('pause', False, 'pause'),
    ('ttl', False, 'ttl'),
    ('storage', False, 'storage_error'),
    ('funding', False, 'funding'),
    ('levels', False, 'levels'),
])
def test_dashboard_readiness_matches_bridge(tmp_path, gate, ready, code):
    store = Store(tmp_path/'analysis.db')
    engine = Engine(Settings(), None, store)
    connected(engine.execution)
    value = row('SHORT')
    value['candidate'] = 'SHORT'
    value['levels'] = dict(entry=100, stop=102, target=96, funding_cost=0.)
    if gate == 'risk_error':
        value['levels']['stop'] = 160
    elif gate == 'pause':
        store.set_paused(True)
    elif gate == 'ttl':
        value['observed_at'] -= 121000
    elif gate == 'storage':
        engine.storage_error = True
    elif gate == 'funding':
        value['levels']['funding_cost'] = None
    elif gate == 'levels':
        value['levels']['stop'] = 98  # Wrong side for SHORT; fail closed at producer.
    engine.rows[value['symbol']] = value
    status = engine.status()
    visible = status['rows'][0]
    assert visible['execution_ready'] is ready
    assert status['actionable_count'] == int(ready)
    assert visible['execution_action'] == ('SHORT' if ready else None)
    if gate == 'levels':
        assert status['bridge']['rejections']['levels'] == 1
    elif code:
        assert code in visible['execution_blocks']
    assert value['decision'] == 'SHORT'  # Status must not change the strategy's analysis.
    store.close()


def test_invalid_pair_is_not_sent_to_executor(tmp_path):
    store = Store(tmp_path/'analysis.db')
    engine = Engine(Settings(), None, store)
    connected(engine.execution)
    value = row(); value['candidate'] = 'LONG'; value['symbol'] = 'BAD-PAIRUSDT'
    engine.rows[value['symbol']] = value
    payload = engine.signals()
    assert payload['signals'] == [] and payload['pairs'] == []
    assert payload['diagnostics']['rejections']['pair'] == 1
    assert not engine.status()['rows'][0]['execution_ready']
    store.close()
