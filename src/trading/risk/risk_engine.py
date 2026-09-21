"""Risk engine: the ONLY order-authorization path.

Consumes P2 score + modifiers, produces position sizing and constraints.
Modifiers may only REDUCE risk (canonical §1.7). Any error -> NO_TRADE.
"""
from __future__ import annotations

from trading.risk.guards import GUARDS


class RiskRejected(Exception):
    """Raised when an order attempt violates a guard or fails closed."""


def compute_position(
    *,
    score: float | None,
    risk_equity: float,
    entry: float,
    stop_distance: float,
    open_notional: float = 0.0,
    modifiers: dict[str, float] | None = None,
    macro_multiplier: float | None = 1.0,
) -> dict:
    """Compute the allowed position. Raises RiskRejected on any violation.

    - score None -> NO_TRADE
    - stop_distance > 5% -> reject
    - risk per trade > 2% of risk_equity -> reject (modifiers only shrink)
    - exposure (open_notional + new) > 30% equity -> reject
    """
    guards = GUARDS

    if score is None:
        raise RiskRejected("NO_TRADE: score is None (missing data)")

    if entry <= 0 or stop_distance <= 0:
        raise RiskRejected(f"invalid entry/stop: entry={entry} stop={stop_distance}")

    if stop_distance > guards.max_stop_distance:
        raise RiskRejected(f"stop distance {stop_distance:.4f} > max {guards.max_stop_distance:.4f}")

    # Base risk 2%; modifiers may only reduce.
    risk_frac = guards.max_risk_per_trade
    if modifiers:
        for name, factor in modifiers.items():
            if not (0.0 < factor <= 1.0):
                raise RiskRejected(f"modifier {name}={factor} outside (0,1]; cannot increase risk")
            risk_frac *= factor
    risk_frac = min(risk_frac, guards.max_risk_per_trade)  # floor at the cap

    if macro_multiplier is not None:
        if not (0.0 < macro_multiplier <= 1.0):
            raise RiskRejected(f"macro_multiplier {macro_multiplier} outside (0,1]")
        risk_frac *= macro_multiplier

    # risk per trade is loss at stop: position_notional * stop_distance = risk.
    # -> position_notional = risk / stop_distance
    risk_amount = risk_equity * risk_frac
    position_notional = risk_amount / stop_distance

    total_notional = open_notional + position_notional
    if total_notional > guards.max_exposure_fraction * risk_equity:
        # Shrink to exposure cap (never exceed); if even 0 notional violates, reject.
        remaining = guards.max_exposure_fraction * risk_equity - open_notional
        if remaining <= 0:
            raise RiskRejected(f"exposure full: open_notional={open_notional:.2f} >= cap")
        position_notional = remaining
        risk_amount = position_notional * stop_distance
        risk_frac = risk_amount / risk_equity if risk_equity > 0 else 0.0
        total_notional = open_notional + position_notional  # recompute after shrink

    return {
        "authorized": True,
        "position_notional": round(position_notional, 6),
        "risk_amount": round(risk_amount, 6),
        "risk_fraction": round(risk_frac, 6),
        "entry": entry,
        "stop_distance": round(stop_distance, 6),
        "stop_price": round(entry * (1 - stop_distance), 6),
        "open_notional": round(open_notional, 6),
        "total_notional": round(total_notional, 6),
    }