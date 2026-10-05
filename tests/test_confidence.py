"""Confidence boundaries, including the shared exit/leverage setting."""
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


@pytest.mark.parametrize('confidence,close_allowed,requested', [(.8499, False, None), (.85, True, None), (.8999, True, None), (.90, True, 3)])
def test_separate_exit_and_leverage_thresholds(tmp_path, confidence, close_allowed, requested):
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
        assert signal.get('leverage_requested') == requested
        assert ('close_trade_id' in signal) == close_allowed
        store.set_paused(True)
        signal = execution.signals([value])['signals'][0]
        assert signal['action'] == 'WAIT'
        assert ('close_trade_id' in signal) == close_allowed
    finally:
        store.close()
