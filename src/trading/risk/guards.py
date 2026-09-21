"""Immutable safety guards (canonical §1.7).

These constants are the contract of the risk layer. Mutation tests assert
their exact values; weakening any of them MUST fail CI. Do not edit without
a human risk-approval recorded in reports/gates/.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Guards:
    # Max loss at protective stop as fraction of CURRENT risk equity.
    max_risk_per_trade: float = 0.02

    # Max stop distance from entry (fraction of entry price).
    max_stop_distance: float = 0.05

    # Daily loss halt (realized + unrealized + fees, UTC day).
    daily_loss_halt: float = -0.05

    # Weekly loss halt (UTC week).
    weekly_loss_halt: float = -0.10

    # Drawdown halt from high-water mark.
    max_drawdown_halt: float = -0.15

    # Portfolio exposure cap (sum of open notional / equity) — leverage safety.
    # 100% = no leveraged stacking in v1; the per-trade 2% rule stays binding.
    max_exposure_fraction: float = 1.0

    # Re-entry sizing after strategy suspension (fraction of normal size).
    reentry_sizing_fraction: float = 0.50

    # Spot only; no margin/leverage in v1.
    allow_leverage: bool = False

    # Modifiers may only reduce risk; this floor is the absolute ceiling.
    modifier_floor: float = 1.0  # 1.0 = modifiers never increase base risk


GUARDS = Guards()


def validate_guards(guards: Guards = GUARDS) -> list[str]:
    """Return a list of violations. Empty list = guards are intact."""
    problems = []
    if guards.max_risk_per_trade > 0.02:
        problems.append(f"max_risk_per_trade {guards.max_risk_per_trade} > 0.02 (weakened)")
    if guards.max_stop_distance > 0.05:
        problems.append(f"max_stop_distance {guards.max_stop_distance} > 0.05 (weakened)")
    if guards.daily_loss_halt > -0.05:
        problems.append(f"daily_loss_halt {guards.daily_loss_halt} > -0.05 (weakened)")
    if guards.weekly_loss_halt > -0.10:
        problems.append(f"weekly_loss_halt {guards.weekly_loss_halt} > -0.10 (weakened)")
    if guards.max_drawdown_halt > -0.15:
        problems.append(f"max_drawdown_halt {guards.max_drawdown_halt} > -0.15 (weakened)")
    if guards.max_exposure_fraction > 1.0:
        problems.append(f"max_exposure_fraction {guards.max_exposure_fraction} > 1.0 (weakened)")
    if guards.allow_leverage:
        problems.append("allow_leverage=True (leverage forbidden in v1)")
    if guards.modifier_floor != 1.0:
        problems.append(f"modifier_floor {guards.modifier_floor} != 1.0 (modifiers must never increase risk)")
    return problems