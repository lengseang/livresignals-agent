"""Leverage learning tests — sizing math, guard rails, backtest sanity."""
from __future__ import annotations

from trading.leverage.sizing import LeverageInputs, leverage_ceiling, size_position
from trading.leverage.backtest import sim_pa_leverage


class TestSizing:
    def test_size_by_risk_not_greed(self):
        r = size_position(LeverageInputs(equity=1000, stop_distance=0.05))
        # risk 2% = $20; / 5% stop = $400 position; margin $20 -> 20x
        assert r.position_size == 400.0
        assert r.leverage == 20.0
        assert r.allowed is False  # 20x is insane -> rejected

    def test_reasonable_leverage_allowed(self):
        r = size_position(LeverageInputs(equity=1000, stop_distance=0.10))
        # risk $20 / 10% stop = $200; margin $20 -> 10x... still >5x -> reject
        assert r.allowed is False

    def test_2x_scenario_allowed(self):
        # Equity 10k, risk 2%, stop 20% -> pos $1000, margin $500 -> 2x
        # target must be >= 2x stop for R:R gate (40% target)
        r = size_position(LeverageInputs(equity=10_000, stop_distance=0.20, target_distance=0.40, margin=500))
        assert r.leverage == 2.0
        assert r.allowed is True

    def test_risk_guard_rejects_over_2pct(self):
        r = size_position(LeverageInputs(equity=1000, risk_pct=0.05))
        assert r.allowed is False
        assert "2% guard" in r.reason

    def test_bad_rr_rejected(self):
        r = size_position(LeverageInputs(equity=10_000, stop_distance=0.10, target_distance=0.10, margin=500))
        assert r.allowed is False
        assert "reward:risk" in r.reason

    def test_liq_cliff_rejects_high_lev(self):
        # 10x on 2% daily vol: liq at -9.5% -> inside normal moves -> reject
        r = size_position(LeverageInputs(equity=10_000, stop_distance=0.10, target_distance=0.20, margin=200))
        assert r.leverage == 10.0
        assert r.allowed is False
        assert "inside normal volatility" in r.reason

    def test_leverage_ceiling_from_volatility(self):
        # 3% daily vol, 3x safety -> max ~11x... but sanity: 1/(3*0.03)=11.1
        assert leverage_ceiling(0.03) <= 12
        # 5% daily vol -> 1/(3*0.05)=6.7
        assert leverage_ceiling(0.05) <= 7
        # extreme 10% vol -> 1/(3*0.10)=3.3
        assert leverage_ceiling(0.10) <= 4


class TestBacktest:
    def test_leverage_cliff_and_funding_bleed(self):
        """The honest finding: leverage monotonically increases RISK —
        liquidations and funding bleed both rise with leverage. Spot (1x)
        pays zero funding; every leveraged level pays more and liquidates
        more. (Final-value ordering is NOT asserted: it is path-dependent.)"""
        closes = [float(c) for c in _fake_closes()]
        r1 = sim_pa_leverage(closes, 1)
        r2 = sim_pa_leverage(closes, 2)
        r10 = sim_pa_leverage(closes, 10)
        # Spot pays no funding; leveraged positions bleed it.
        assert r1["funding_paid"] == 0.0
        assert r2["funding_paid"] > 0.0
        assert r10["funding_paid"] > 0.0
        # Risk stacks with leverage: more liquidations at higher leverage.
        assert r10["liquidations"] > r2["liquidations"]
        # 10x liquidates at least once in a series with -30% crashes
        # (the whole reason high leverage is dangerous).
        assert r10["liquidations"] >= 1


def _fake_closes() -> list[float]:
    """Synthetic trending+crash series (fast, deterministic, no DB)."""
    import math
    out = []
    price = 100.0
    for i in range(1000):
        # gentle drift + two -30% crashes
        if i in (300, 700):
            price *= 0.7
        else:
            price *= 1.001 + 0.004 * math.sin(i / 30)
        out.append(price)
    return out
