"""Economic invariants, rather than snapshots of the implementation."""
import math
import pytest
from app.risk import RiskPolicy, adverse_funding, loss_fraction, choose_leverage, size_margin, net_rr, daily_loss_hit, cooldown_active

P = RiskPolicy()


@pytest.mark.parametrize('equity', [50., 2000., 50000.])
@pytest.mark.parametrize('stop', [99.95, 99.8, 98., 80.])
@pytest.mark.parametrize('requested', [1, 5, 100])
def test_risk_budget_and_margin_invariants(equity, stop, requested):
    loss = loss_fraction(100., stop, .0003, P)
    leverage = choose_leverage(loss, requested, 125., P)
    stake = size_margin(equity, loss, leverage, equity, 0., P)
    assert stake <= equity*.07 + 1e-9
    assert stake*leverage*loss <= equity*.005 + 1e-9
    assert 1 <= leverage <= min(20, requested)
    assert leverage*loss <= .50


def test_funding_never_credits_future_income():
    assert adverse_funding(.001, 'LONG', 1) == .001
    assert adverse_funding(.001, 'SHORT', 1) == 0
    assert adverse_funding(-.001, 'SHORT', 2) == .002
    assert net_rr(100, 98, 104, .001, P) < net_rr(100, 98, 104, 0, P)
    assert loss_fraction(100, 98, .001, P) > loss_fraction(100, 98, 0, P)


@pytest.mark.parametrize('bad', [math.nan, math.inf, -1., 0., True])
def test_invalid_equity_blocks_size_and_daily_limit(bad):
    assert size_margin(bad, .02, 2., 2000., 0., P) == 0
    assert daily_loss_hit(bad, 0., 0., P)


def test_daily_limit_uses_current_equity_and_open_losses():
    assert not daily_loss_hit(4000., -61., 0., P)  # Not the initial $2000 wallet.
    assert daily_loss_hit(1000., -31., 0., P)
    assert daily_loss_hit(2000., -20., -40., P)
    assert daily_loss_hit(2000., -61., 100., P)  # Open gains cannot erase realized breach.
    assert not daily_loss_hit(2000., -59., 0., P)


def test_minimum_stake_and_impossible_loss_block():
    assert size_margin(2000, .6, 1, 2000, 0, P) == 0
    assert choose_leverage(.6, 100, 100, P) == 0
    assert size_margin(2000, .02, 1, 2000, 141, P) == 0
    assert cooldown_active(1000, 300999, P)
    assert not cooldown_active(1000, 301000, P)


@pytest.mark.parametrize('overrides', [dict(capital_risk=.006), dict(margin_fraction=.08), dict(max_leverage=21),
    dict(daily_loss=.04), dict(cost_per_side=0), dict(min_rr=1), dict(funding_periods=True)])
def test_policy_cannot_relax_foundation(overrides):
    with pytest.raises(ValueError): RiskPolicy(**overrides)
