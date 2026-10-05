"""Confidence boundaries, with separate entry and exit policies."""
import pytest

from app.config import Settings
from app.execution import Execution
from app.store import Store
from app.strategy import decide
from test_analysis import answer
from test_execution import connected, row


def test_default_and_explicit_environment_confidence(monkeypatch):
    monkeypatch.setattr('app.config.load_dotenv', lambda: None)
    monkeypatch.delenv('MIN_AI_CONFIDENCE', raising=False)
    assert Settings().min_confidence == Settings.load().min_confidence == .90
    monkeypatch.setenv('MIN_AI_CONFIDENCE', '.95')
    assert Settings.load().min_confidence == .95


@pytest.mark.parametrize('direction', ['LONG', 'SHORT'])
@pytest.mark.parametrize('field', ['direction', 'momentum', 'regime', 'risk'])
@pytest.mark.parametrize('confidence,allowed', [(.85, False), (.8999, False), (.90, True)])
def test_each_entry_assessment_requires_ninety_percent(direction, field, confidence, allowed):
    ai = answer(direction)
    ai['answers'][field]['confidence'] = confidence
    result = dict(candidate=direction, score=100, guards=[dict(label='spread', passed=True)])
    assert decide(result, ai, Settings())[0] == (direction if allowed else 'WAIT')


@pytest.mark.parametrize('confidence,close_allowed', [(.8499, False), (.85, True), (.8999, True), (.90, True)])
def test_exit_confidence_is_independent_of_risk_sizing(tmp_path, confidence, close_allowed):
    store = Store(tmp_path / 'confidence.db')
    try:
        execution = Execution(Settings(), None, store)
        connected(execution)
        value = row()
        value['position_id'] = 42
        value['ai']['answers'] = {
            'leverage': {'choice': '3', 'confidence': confidence},
            'position_action': {'choice': 'CLOSE', 'confidence': confidence},
        }
        signal = execution.signals([value])['signals'][0]
        assert signal['action'] == 'LONG' and signal['leverage_requested'] == 4
        assert ('close_trade_id' in signal) == close_allowed
        store.set_paused(True)
        signal = execution.signals([value])['signals'][0]
        assert signal['action'] == 'WAIT'
        assert ('close_trade_id' in signal) == close_allowed
    finally:
        store.close()
