"""Circuit breakers (canonical §1.6): per-strategy + portfolio + emergency.

Evaluates INTRADAY marked equity (realized + unrealized + fees), not just
closed trades. Halt cancels new orders and preserves protective orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from trading.risk.guards import GUARDS


@dataclass
class EquitySnapshot:
    """Marked equity at a point in time."""

    utc: datetime
    equity: float  # marked equity incl. unrealized
    realized_pnl: float  # today's realized
    unrealized_pnl: float  # today's unrealized
    fees: float  # today's fees (positive = cost)


@dataclass
class BreakerState:
    halted: bool = False
    reason: str | None = None
    halted_at_utc: str | None = None


def _utc_day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)


def _utc_week_start(now: datetime) -> datetime:
    day = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    return day.replace(day=day.day - day.weekday())


def check_portfolio_breakers(
    day_start_equity: float,
    week_start_equity: float,
    high_water_mark: float,
    snapshot: EquitySnapshot,
) -> BreakerState:
    """Check daily/weekly/drawdown breakers against marked equity.

    day_start_equity / week_start_equity are the marked equities at the start
    of the current UTC day / week; high_water_mark is the all-time peak.
    """
    pnl_day = snapshot.equity - day_start_equity
    pnl_week = snapshot.equity - week_start_equity
    drawdown = snapshot.equity - high_water_mark

    if day_start_equity > 0 and pnl_day / day_start_equity <= GUARDS.daily_loss_halt:
        return BreakerState(True, f"daily_halt pnl%={pnl_day / day_start_equity:.4f}", snapshot.utc.isoformat())
    if week_start_equity > 0 and pnl_week / week_start_equity <= GUARDS.weekly_loss_halt:
        return BreakerState(True, f"weekly_halt pnl%={pnl_week / week_start_equity:.4f}", snapshot.utc.isoformat())
    if high_water_mark > 0 and drawdown / high_water_mark <= GUARDS.max_drawdown_halt:
        return BreakerState(True, f"drawdown_halt dd%={drawdown / high_water_mark:.4f}", snapshot.utc.isoformat())
    return BreakerState()


def check_strategy_breakers(
    *,
    strategy_loss_pct: float,
    consecutive_losses: int,
    daily_loss_budget_used: float,
) -> BreakerState:
    """Per-strategy 5/3/2 suspension (canonical §1.6, finalized).

    Suspended when: strategy loss >= 5%, OR 3 consecutive losses,
    OR 2x daily loss budget used. No hidden global three-loss rule.
    """
    if strategy_loss_pct <= -0.05:
        return BreakerState(True, f"strategy_loss {strategy_loss_pct:.4f} <= -5%")
    if consecutive_losses >= 3:
        return BreakerState(True, f"consecutive_losses {consecutive_losses} >= 3")
    if daily_loss_budget_used >= 2.0:
        return BreakerState(True, f"daily_loss_budget_used {daily_loss_budget_used:.2f} >= 2x")
    return BreakerState()


def reentry_size(normal_size: float, halts_elapsed: int = 0) -> float:
    """50% re-entry sizing after suspension (canonical §1.6)."""
    if halts_elapsed <= 0:
        return normal_size
    return normal_size * GUARDS.reentry_sizing_fraction