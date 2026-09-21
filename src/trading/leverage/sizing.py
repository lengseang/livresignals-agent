"""Leverage learning toolkit — sizing calculator + honest backtest.

The rule that makes leverage survivable:
  leverage is a RESULT of position sizing, never a choice.

  risk = 1-2% of equity (guard, unchanged)
  position_size = risk / stop_distance
  leverage = position_size / margin
  reward:risk must be >= 2:1

This module also computes the liquidation cliff (liq price vs expected
volatility) and REFUSES leverage levels where a normal daily move can
trigger liquidation — the math that killed 5x/10x in the 9yr sim.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LeverageInputs:
    equity: float            # account equity (USD)
    risk_pct: float = 0.02   # risk per trade (guard: never > 0.02)
    stop_distance: float = 0.05   # stop distance as fraction (e.g. 0.05 = 5%)
    target_distance: float = 0.10 # target as fraction (for R:R check)
    margin: float | None = None   # margin you plan to put up (None = auto)
    maintenance_margin: float = 0.005  # Binance futures ~0.5%
    fee_rate: float = 0.0004         # taker fee ~0.04% per side


@dataclass(frozen=True)
class LeverageResult:
    position_size: float
    leverage: float
    liq_price_pct: float     # distance to liquidation as fraction
    reward_risk: float
    allowed: bool
    reason: str
    fees_round_trip: float


def size_position(inp: LeverageInputs) -> LeverageResult:
    """Compute the ONLY safe position size, then the resulting leverage.

    If a provided margin implies leverage beyond the safety envelope, the
    result is marked NOT allowed with the reason.
    """
    # Position sized by RISK, not by greed.
    risk_amount = inp.equity * inp.risk_pct
    position_size = risk_amount / inp.stop_distance if inp.stop_distance > 0 else 0.0

    margin = inp.margin if inp.margin is not None else risk_amount
    leverage = position_size / margin if margin > 0 else 0.0

    # Liquidation cliff: how far price must fall to liquidate.
    # For a long at L leverage (isolated): liq ≈ entry × (1 - 1/L + m).
    # Distance = -(1/L - m). 10x -> -9.5%, 5x -> -19.5%, 2x -> -49.5%.
    liq_pct = -(1 / leverage - inp.maintenance_margin) if leverage > 1 else -1.0

    # Reward:risk gate.
    rr = inp.target_distance / inp.stop_distance if inp.stop_distance > 0 else 0.0

    allowed = True
    reason = "ok"
    if inp.risk_pct > 0.02:
        allowed, reason = False, "risk_pct exceeds 2% guard"
    elif rr < 2.0:
        allowed, reason = False, f"reward:risk {rr:.1f} < 2.0 (bad trade shape)"
    elif leverage > 1 and abs(liq_pct) < 0.10:
        allowed, reason = False, f"leverage {leverage:.1f}x -> liq at {liq_pct*100:.1f}% — inside normal volatility (reject)"
    elif leverage > 5:
        allowed, reason = False, f"leverage {leverage:.1f}x > 5x hard ceiling (reject)"

    fees = position_size * inp.fee_rate * 2  # round trip
    return LeverageResult(
        position_size=round(position_size, 2),
        leverage=round(leverage, 2),
        liq_price_pct=round(liq_pct, 4),
        reward_risk=round(rr, 2),
        allowed=allowed,
        reason=reason,
        fees_round_trip=round(fees, 4),
    )


def leverage_ceiling(volatility_daily: float, maintenance: float = 0.005,
                     safety_multiple: float = 3.0) -> float:
    """Max leverage such that a `safety_multiple` x daily-vol move does not
    liquidate. This is the REAL ceiling — volatility decides, not ambition.

    liq move ≈ 1/lev (ignoring maintenance). Require:
        1/lev >= safety_multiple * daily_vol
      => lev <= 1 / (safety_multiple * daily_vol)
    """
    if volatility_daily <= 0:
        return 1.0
    return round(1.0 / (safety_multiple * volatility_daily), 2)
