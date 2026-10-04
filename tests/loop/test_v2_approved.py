"""The owner-approved paper limits are pinned: any change must change this test, on purpose."""
import pathlib

import pytest

from trading.risk.guards import GUARDS
from trading.v2 import approved as A
from trading.v2.approved import approved_config

SYMS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def test_every_approved_number_is_exactly_what_the_owner_approved():
    c = approved_config(SYMS, 15, pathlib.Path("x"))
    assert c.paper_equity == 1000.0 and c.cadence_minutes == 15 and c.symbols == SYMS
    assert (c.exit_params.stop_pct, c.exit_params.target_pct) == (0.035, 0.07)
    assert c.risk_limits.daily_loss_cap_usd == 20.0 and c.risk_limits.max_spread_bps == 5.0
    k = c.kill_limits
    assert (k.daily_loss_cap_usd, k.max_price_move_frac, k.exchange_failures_to_engage, k.max_feed_age_ms,
            k.max_abs_slippage_bps, k.min_request_confidence) == (40.0, 0.03, 3, 180_000, 50.0, 0.8)
    assert c.daily_loss_budget_usd == 20.0
    assert (c.costs.fee_rate, c.costs.spread_bps, c.costs.slippage_bps) == (0.001, 0.82, 1.0)
    assert A.APPROVED_ON == "2026-10-04"


def test_the_approved_limits_respect_the_legacy_guards_and_each_other():
    c = approved_config(SYMS, 15, pathlib.Path("x"))
    assert c.exit_params.stop_pct <= GUARDS.max_stop_distance                   # never wider than the legacy 5%
    assert c.risk_limits.daily_loss_cap_usd / c.paper_equity < abs(GUARDS.daily_loss_halt)   # tighter than the legacy 5% halt
    assert c.kill_limits.daily_loss_cap_usd == 2 * c.risk_limits.daily_loss_cap_usd         # emergency stop = 2x risk cap
    assert c.exit_params.target_pct == pytest.approx(2 * c.exit_params.stop_pct)
    # the cost buffer must cover the approved fee + slippage compounded (RiskLimits enforces it at construction)
    assert c.risk_limits.cost_buffer_frac >= (1 + c.costs.fee_rate) * (1 + c.risk_limits.max_entry_slippage_bps / 1e4) - 1


def test_order_rate_limit_scales_with_symbols_but_never_below_five():
    assert approved_config(("BTCUSDT",), 15, pathlib.Path("x")).kill_limits.max_orders_per_minute == 5
    assert approved_config(tuple(f"S{i}USDT" for i in range(6)), 15, pathlib.Path("x")).kill_limits.max_orders_per_minute == 12


def test_the_preset_is_constants_not_a_runtime_calculation():
    src = pathlib.Path(A.__file__).read_text()
    assert "calibrate" not in src.replace("python -m trading.v2.calibrate", "") and "BinancePublic" not in src
    assert "import time" not in src and "urllib" not in src


def test_a_stop_out_at_the_approved_numbers_uses_about_the_daily_cap():
    """The cap was sized so one full stop-out is ~ the daily limit: 2% risk per trade on $1000 = $20."""
    risk_per_trade = GUARDS.max_risk_per_trade * A.PAPER_EQUITY_USD
    assert risk_per_trade == pytest.approx(A.DAILY_LOSS_CAP_USD) == pytest.approx(A.DAILY_LOSS_BUDGET_USD)
