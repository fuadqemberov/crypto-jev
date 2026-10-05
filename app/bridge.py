"""Strict protocol shared by the engine and the paper executor."""
import re
from typing import Any
from .risk import finite

VERSION = 3
MAX_AGE_MS = 15_000
MAX_PAYLOAD_BYTES = 1_000_000
PAIR = re.compile(r'^[A-Z0-9]+/USDT:USDT$')
ID = re.compile(r'^[a-f0-9]{24}$')


def fresh(signal: dict[str, Any], now: float) -> bool:
    observed, expires = signal.get('observed_at'), signal.get('expires_at')
    return (finite(now) and finite(observed) and finite(expires) and observed <= now < expires
            and 0 < expires - observed <= 300_000)


def validate_payload(payload: Any, now: float, policy_id: str) -> None:
    if (not isinstance(payload, dict) or type(payload.get('version')) is not int or payload['version'] != VERSION
            or payload.get('mode') != 'dry_run' or payload.get('risk_policy') != policy_id
            or not finite(payload.get('generated_at')) or not 0 <= now - payload['generated_at'] <= MAX_AGE_MS
            or type(payload.get('entries_enabled')) is not bool or not isinstance(payload.get('signals'), list)
            or len(payload['signals']) > 5000):
        raise ValueError('Invalid bridge envelope')


def signal_error(signal: Any, now: float) -> str | None:
    if not isinstance(signal, dict):
        return 'schema'
    if not fresh(signal, now):
        return 'ttl'
    if not isinstance(signal.get('id'), str) or not ID.fullmatch(signal['id']):
        return 'identity'
    if not isinstance(signal.get('pair'), str) or not PAIR.fullmatch(signal['pair']):
        return 'pair'
    if signal.get('action') not in ('WAIT', 'LONG', 'SHORT'):
        return 'action'
    if 'close_trade_id' in signal and (type(signal['close_trade_id']) is not int or signal['close_trade_id'] <= 0):
        return 'position'
    if signal['action'] != 'WAIT':
        if type(signal.get('leverage_requested')) is not int or signal['leverage_requested'] not in (1,2,3,5,10,15,20,25,50,75,100):
            return 'leverage'
        levels = signal.get('levels')
        if not isinstance(levels, dict) or not all(finite(levels.get(k)) and levels[k] > 0 for k in ('entry','stop','target')):
            return 'levels'
        stop, entry, target = (levels[k] for k in ('stop', 'entry', 'target'))
        if not (stop < entry < target if signal['action'] == 'LONG' else target < entry < stop):
            return 'levels'
        if not finite(signal.get('funding_cost')) or not 0 <= signal['funding_cost'] <= .12:
            return 'funding'
    return None
