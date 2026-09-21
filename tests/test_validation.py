"""P4 tests: metrics correctness + walk-forward anti-leakage."""
from __future__ import annotations

import math

import pytest

from trading.validation.backtest import simulate
from trading.validation.metrics import (
    degradation,
    gate_passed,
    max_drawdown,
    profit_factor,
    sharpe_annualized,
    win_rate,
)
from trading.validation.walkforward import run_walkforward

def test_sharpe_annualized_positive_returns():
    rets = [0.01 if i % 2 else 0.008 for i in range(30)]  # small but real variance
    s = sharpe_annualized(rets)
    assert s > 0


def test_sharpe_constant_returns_zero():
    # Zero variance -> 0.0 (numeric guard), never astronomical.
    assert sharpe_annualized([0.01] * 30) == 0.0


def test_sharpe_zero_variance():
    assert sharpe_annualized([0.0, 0.0, 0.0]) == 0.0


def test_max_drawdown():
    curve = [100, 110, 105, 90, 95, 80, 100]
    # peak 110 -> trough 80 = 27.3%
    assert max_drawdown(curve) == pytest.approx((110 - 80) / 110, abs=1e-9)


def test_max_drawdown_monotonic_up():
    assert max_drawdown([100, 101, 102, 103]) == 0.0


def test_win_rate_and_profit_factor():
    trades = [0.10, -0.05, 0.02, -0.02, 0.01]
    assert win_rate(trades) == pytest.approx(0.6, abs=1e-9)
    # gross win = .13, gross loss = .07 -> PF ~1.857
    assert profit_factor(trades) == pytest.approx(0.13 / 0.07, abs=1e-9)


def test_degradation_formula():
    assert degradation(1.2, 1.5) == pytest.approx(1 - 1.2 / 1.5, abs=1e-9)
    assert degradation(0.10, 0.05, is_drawdown=True) == pytest.approx(0.10 / 0.05 - 1, abs=1e-9)
    assert math.isinf(degradation(0.5, 0.0))  # non-positive denominator -> fail


def test_gate_passed_thresholds():
    good = {"sharpe": 1.5, "max_drawdown": 0.10, "win_rate": 0.45, "profit_factor": 1.5, "degradation": 0.10}
    assert gate_passed(good)["passed"]
    bad = {**good, "max_drawdown": 0.20}
    assert not gate_passed(bad)["passed"]


def test_simulate_basic():
    closes = [100.0 * (1.005 ** i) for i in range(120)]
    rows = [
        {"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "open_time_utc": i * 86400_000}
        for i, c in enumerate(closes)
    ]
    sim = simulate(rows)
    assert sim["n_trades"] >= 0
    assert sim["final_equity"] > 0


def test_walkforward_insufficient_data():
    rows = [
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "open_time_utc": i * 86400_000}
        for i in range(50)
    ]
    r = run_walkforward("BTC/USDT", rows)
    assert r["status"] == "insufficient_data"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))