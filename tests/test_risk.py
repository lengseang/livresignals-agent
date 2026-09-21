"""P3 tests: risk engine, immutable guards, breakers."""
from __future__ import annotations

import pytest

from trading.risk.breakers import (
    EquitySnapshot,
    check_portfolio_breakers,
    check_strategy_breakers,
    reentry_size,
)
from trading.risk.guards import GUARDS, Guards, validate_guards
from trading.risk.risk_engine import RiskRejected, compute_position


# ---------- immutable guards ----------

def test_guards_intact():
    assert validate_guards(GUARDS) == []


def test_mutation_weakened_risk_fails():
    mutated = Guards(max_risk_per_trade=0.05)
    assert validate_guards(mutated)  # non-empty = violation detected


def test_mutation_weakened_stop_fails():
    mutated = Guards(max_stop_distance=0.10)
    assert validate_guards(mutated)


def test_mutation_leverage_enabled_fails():
    mutated = Guards(allow_leverage=True)
    assert validate_guards(mutated)


def test_mutation_exposure_cap_weakened_fails():
    mutated = Guards(max_exposure_fraction=1.5)
    assert validate_guards(mutated)


def test_mutation_modifier_floor_raises_risk_fails():
    mutated = Guards(modifier_floor=1.5)
    assert validate_guards(mutated)


# ---------- risk engine ----------

def test_risk_engine_basic_sizing():
    r = compute_position(score=60.0, risk_equity=10_000.0, entry=100.0, stop_distance=0.02)
    assert r["authorized"]
    # risk = 2% of 10k = 200; notional = 200 / 0.02 = 10,000
    assert r["position_notional"] == pytest.approx(10_000.0, abs=1e-6)
    assert r["risk_amount"] == pytest.approx(200.0, abs=1e-6)


def test_risk_engine_modifiers_only_reduce():
    r = compute_position(
        score=60.0, risk_equity=10_000.0, entry=100.0, stop_distance=0.02,
        modifiers={"kelly": 0.5},
    )
    # 2% * 0.5 = 1% -> risk 100 -> notional 5,000
    assert r["position_notional"] == pytest.approx(5_000.0, abs=1e-6)


def test_risk_engine_modifier_above_1_rejected():
    with pytest.raises(RiskRejected):
        compute_position(score=60.0, risk_equity=10_000.0, entry=100.0, stop_distance=0.02, modifiers={"kelly": 1.5})


def test_risk_engine_none_score_no_trade():
    with pytest.raises(RiskRejected, match="NO_TRADE"):
        compute_position(score=None, risk_equity=10_000.0, entry=100.0, stop_distance=0.02)


def test_risk_engine_stop_too_wide_rejected():
    with pytest.raises(RiskRejected, match="stop distance"):
        compute_position(score=60.0, risk_equity=10_000.0, entry=100.0, stop_distance=0.07)


def test_risk_engine_exposure_cap():
    # cap = 100% of 10k = 10,000. open 8,000 -> only 2,000 room (stacking guard).
    r = compute_position(score=60.0, risk_equity=10_000.0, entry=100.0, stop_distance=0.02, open_notional=8_000.0)
    assert r["total_notional"] == pytest.approx(10_000.0, abs=1e-6)
    assert r["position_notional"] == pytest.approx(2_000.0, abs=1e-6)


def test_risk_engine_exposure_full_rejected():
    with pytest.raises(RiskRejected, match="exposure full"):
        compute_position(score=60.0, risk_equity=10_000.0, entry=100.0, stop_distance=0.02, open_notional=10_200.0)


def test_risk_engine_macro_reduces_only():
    r = compute_position(score=60.0, risk_equity=10_000.0, entry=100.0, stop_distance=0.02, macro_multiplier=0.5)
    assert r["position_notional"] == pytest.approx(5_000.0, abs=1e-6)


# ---------- breakers ----------

def test_daily_breaker_intraday_marked_equity():
    now = __import__("datetime").datetime(2026, 8, 11, 12, 0, tzinfo=__import__("datetime").timezone.utc)
    snap = EquitySnapshot(utc=now, equity=9_400.0, realized_pnl=-600.0, unrealized_pnl=0.0, fees=10.0)
    state = check_portfolio_breakers(day_start_equity=10_000.0, week_start_equity=10_000.0, high_water_mark=10_500.0, snapshot=snap)
    assert state.halted
    assert "daily_halt" in state.reason


def test_weekly_breaker():
    now = __import__("datetime").datetime(2026, 8, 11, 12, 0, tzinfo=__import__("datetime").timezone.utc)
    snap = EquitySnapshot(utc=now, equity=8_900.0, realized_pnl=0.0, unrealized_pnl=-1100.0, fees=0.0)
    state = check_portfolio_breakers(day_start_equity=9_000.0, week_start_equity=10_000.0, high_water_mark=10_000.0, snapshot=snap)
    assert state.halted
    assert "weekly_halt" in state.reason


def test_drawdown_breaker():
    now = __import__("datetime").datetime(2026, 8, 11, 12, 0, tzinfo=__import__("datetime").timezone.utc)
    snap = EquitySnapshot(utc=now, equity=8_400.0, realized_pnl=0.0, unrealized_pnl=0.0, fees=0.0)
    state = check_portfolio_breakers(day_start_equity=8_500.0, week_start_equity=8_500.0, high_water_mark=10_000.0, snapshot=snap)
    assert state.halted
    assert "drawdown_halt" in state.reason


def test_no_breaker_normal():
    now = __import__("datetime").datetime(2026, 8, 11, 12, 0, tzinfo=__import__("datetime").timezone.utc)
    snap = EquitySnapshot(utc=now, equity=10_100.0, realized_pnl=100.0, unrealized_pnl=0.0, fees=5.0)
    state = check_portfolio_breakers(day_start_equity=10_000.0, week_start_equity=10_000.0, high_water_mark=10_100.0, snapshot=snap)
    assert not state.halted


def test_strategy_5_3_2():
    assert check_strategy_breakers(strategy_loss_pct=-0.06, consecutive_losses=0, daily_loss_budget_used=0.0).halted
    assert check_strategy_breakers(strategy_loss_pct=-0.01, consecutive_losses=3, daily_loss_budget_used=0.0).halted
    assert check_strategy_breakers(strategy_loss_pct=-0.01, consecutive_losses=1, daily_loss_budget_used=2.1).halted
    assert not check_strategy_breakers(strategy_loss_pct=-0.01, consecutive_losses=1, daily_loss_budget_used=0.5).halted


def test_reentry_half_size():
    assert reentry_size(10_000.0, halts_elapsed=1) == pytest.approx(5_000.0, abs=1e-6)
    assert reentry_size(10_000.0, halts_elapsed=0) == pytest.approx(10_000.0, abs=1e-6)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))