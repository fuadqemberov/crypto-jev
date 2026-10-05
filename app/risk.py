"""Shared, pure risk policy. No directional decisions and no exchange I/O."""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any


def finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class RiskPolicy:
    capital_risk: float = .005
    margin_fraction: float = .07
    margin_loss: float = .50
    max_leverage: int = 20
    daily_loss: float = .03
    min_rr: float = 1.5
    max_spread: float = 15.
    max_slippage: float = .003
    cost_per_side: float = .0008
    funding_periods: int = 1
    cooldown_seconds: int = 300

    def __post_init__(self) -> None:
        bounds = {'capital_risk': (.00001, .005), 'margin_fraction': (.001, .07),
                  'margin_loss': (.01, .50), 'daily_loss': (.001, .03),
                  'min_rr': (1.5, 10), 'max_spread': (.1, 100),
                  'max_slippage': (.00001, .003), 'cost_per_side': (.0008, .01)}
        for key, (low, high) in bounds.items():
            value = getattr(self, key)
            if not finite(value) or not low <= value <= high:
                raise ValueError(f'Invalid risk policy: {key}')
        for key, low, high in [('max_leverage', 1, 20), ('funding_periods', 0, 12), ('cooldown_seconds', 300, 86400)]:
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'Invalid risk policy: {key}')

    @property
    def identity(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]


def adverse_funding(rate: float, side: str, periods: int) -> float:
    if not finite(rate) or side not in ('LONG', 'SHORT') or type(periods) is not int or periods < 0:
        raise ValueError('Invalid funding data')
    return max(0., rate * (1 if side == 'LONG' else -1)) * periods


def loss_fraction(entry: float, stop: float, funding: float, policy: RiskPolicy) -> float:
    if not all(finite(x) and x > 0 for x in (entry, stop)) or not finite(funding) or funding < 0:
        raise ValueError('Invalid risk inputs')
    return abs(entry - stop) / entry + policy.cost_per_side * (1 + stop / entry) + funding


def net_rr(entry: float, stop: float, target: float, funding: float, policy: RiskPolicy) -> float:
    if not finite(target) or target <= 0 or not (stop < entry < target or target < entry < stop):
        raise ValueError('Invalid stop/target geometry')
    reward = abs(target - entry) / entry - policy.cost_per_side * (1 + target / entry) - funding
    return reward / loss_fraction(entry, stop, funding, policy)


def choose_leverage(loss: float, requested: int, exchange_max: float, policy: RiskPolicy) -> float:
    if not finite(loss) or loss <= 0 or type(requested) is not int or requested < 1 or not finite(exchange_max) or exchange_max < 1:
        raise ValueError('Invalid leverage inputs')
    # Smallest integer leverage that can use the risk budget within the margin cap.
    target = math.ceil(policy.capital_risk / (policy.margin_fraction * loss) - 1e-12)
    cap = math.floor(min(requested, exchange_max, policy.max_leverage, policy.margin_loss / loss))
    return float(min(max(1, target), cap)) if cap >= 1 else 0.


def size_margin(equity: float, loss: float, leverage: float, available: float,
                minimum: float, policy: RiskPolicy) -> float:
    if not all(finite(x) and x > 0 for x in (equity, loss, leverage, available)) or not finite(minimum) or minimum < 0:
        return 0.
    if leverage > policy.max_leverage or loss * leverage > policy.margin_loss:
        return 0.
    stake = min(equity * policy.margin_fraction, equity * policy.capital_risk / (loss * leverage), available)
    return stake if stake >= minimum else 0.


def daily_loss_hit(equity: float, realized_today: float, unrealized: float, policy: RiskPolicy) -> bool:
    """UTC-day realized + current open loss against CURRENT marked equity; no reset on restart."""
    if not all(finite(x) for x in (equity, realized_today, unrealized)) or equity <= 0:
        return True
    return realized_today + min(0., unrealized) <= -equity * policy.daily_loss


def cooldown_active(last_close_ms: float | None, now_ms: float, policy: RiskPolicy) -> bool:
    return last_close_ms is not None and (not finite(last_close_ms) or not finite(now_ms)
                                         or now_ms - last_close_ms < policy.cooldown_seconds * 1000)
