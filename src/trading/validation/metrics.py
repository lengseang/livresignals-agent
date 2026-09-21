"""Walk-forward validation metrics (canonical §2.8, five hard metrics)."""
from __future__ import annotations

import math


def sharpe_annualized(daily_returns: list[float], risk_free: float = 0.0) -> float:
    """Sharpe from daily returns, annualized (sqrt 365). Empty/single -> 0.

    Numeric guard: near-zero variance (std < 1e-12) yields 0.0 rather than a
    meaningless astronomical value from division by ~1e-17.
    """
    if len(daily_returns) < 2:
        return 0.0
    mean = sum(daily_returns) / len(daily_returns)
    var = sum((r - mean) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
    std = math.sqrt(max(var, 0.0))
    if std < 1e-12:
        return 0.0
    return (mean - risk_free) / std * math.sqrt(365)


def max_drawdown(equity_curve: list[float]) -> float:
    """Max drawdown as positive fraction (0.15 = 15% drop)."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    mdd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > mdd:
                mdd = dd
    return mdd


def win_rate(trade_returns: list[float]) -> float:
    """Fraction of trades with positive net return (after costs)."""
    if not trade_returns:
        return 0.0
    return sum(1.0 for r in trade_returns if r > 0) / len(trade_returns)


def profit_factor(trade_returns: list[float]) -> float:
    """Gross profit / gross loss. No winners or no losers -> 0."""
    gross_win = sum(r for r in trade_returns if r > 0)
    gross_loss = abs(sum(r for r in trade_returns if r < 0))
    if gross_loss <= 0:
        return 0.0 if gross_win <= 0 else float("inf")
    return gross_win / gross_loss


def degradation(test_metric: float, train_metric: float, is_drawdown: bool = False) -> float:
    """Canonical degradation. Non-positive denominator -> +inf (gate fails)."""
    if is_drawdown:
        if train_metric <= 0:
            return float("inf")
        return test_metric / train_metric - 1.0
    if train_metric <= 0:
        return float("inf")
    return 1.0 - (test_metric / train_metric)


def gate_passed(metrics: dict) -> dict:
    """Evaluate the five hard metrics. Returns pass/fail detail."""
    thresholds = {
        "sharpe": 1.0,
        "max_drawdown": 0.15,  # max allowed
        "win_rate": 0.40,
        "profit_factor": 1.3,
        "degradation": 0.20,  # max allowed
    }
    checks = {
        "sharpe": metrics["sharpe"] > thresholds["sharpe"],
        "max_drawdown": metrics["max_drawdown"] < thresholds["max_drawdown"],
        "win_rate": metrics["win_rate"] > thresholds["win_rate"],
        "profit_factor": metrics["profit_factor"] > thresholds["profit_factor"],
        "degradation": metrics["degradation"] <= thresholds["degradation"],
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "thresholds": thresholds,
        "metrics": metrics,
    }