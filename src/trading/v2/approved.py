"""The OWNER-APPROVED V2 limits for PAPER use (approved 2026-10-04: "approve all" of docs/v2/PROPOSED-LIMITS.md).

Fixed constants, deliberately NOT recomputed at runtime: a limit that silently moves with the market is not a limit. To change a
number, change it here with a reason and a test; `python -m trading.v2.calibrate` shows what the market data would suggest today.

SCOPE: PAPER ONLY. Real-money use is a separate decision that has not been made. Fees and slippage remain UNKNOWN placeholders
until real fills exist, so paper results flatter reality by that gap.
"""
from __future__ import annotations

from pathlib import Path

from trading.replay.broker import CostModel
from trading.v2.killswitch import KillLimits
from trading.v2.policy import ExitParams
from trading.v2.risk import RiskLimits
from trading.v2.runner import RunnerConfig

APPROVED_ON = "2026-10-04"
PAPER_EQUITY_USD = 1_000.0
# rule (see PROPOSED-LIMITS.md)                                   value
STOP_PCT = 0.035                  # 3 x p90 hourly ATR of the widest symbol, clamped 1%..5%
TARGET_PCT = 0.07                 # 2 x stop
DAILY_LOSS_CAP_USD = 20.0         # 2% of paper equity (risk engine); tighter than the legacy 5% halt
KILL_DAILY_LOSS_CAP_USD = 40.0    # 4%: the emergency stop is twice the risk cap
KILL_MAX_PRICE_MOVE_FRAC = 0.03   # 3 x the largest 1-minute move seen (0.84%), clamped 2%..15%
RISK_MAX_SPREAD_BPS = 5.0         # max(5, 4 x worst median spread)
PAPER_SPREAD_BPS = 0.82           # worst per-symbol median spread sampled (SOL)
PAPER_SLIPPAGE_BPS = 1.0          # an assumption: no real fills yet
FEE_RATE = 0.001                  # the repo CostModel's 0.10%; the account's real rate is UNKNOWN
DAILY_LOSS_BUDGET_USD = 20.0      # one full stop-out at the legacy 2% risk per trade (E8)
KILL_EXCHANGE_FAILURES = 3
KILL_MAX_FEED_AGE_MS = 180_000
KILL_MIN_REQUEST_CONFIDENCE = 0.8
KILL_MAX_ABS_SLIPPAGE_BPS = 50.0


def approved_config(symbols: tuple[str, ...], cadence_minutes: int, out_dir: Path) -> RunnerConfig:
    return RunnerConfig(
        symbols=symbols, cadence_minutes=cadence_minutes, paper_equity=PAPER_EQUITY_USD, out_dir=out_dir,
        risk_limits=RiskLimits(daily_loss_cap_usd=DAILY_LOSS_CAP_USD, max_spread_bps=RISK_MAX_SPREAD_BPS),
        kill_limits=KillLimits(exchange_failures_to_engage=KILL_EXCHANGE_FAILURES, max_feed_age_ms=KILL_MAX_FEED_AGE_MS,
                               max_orders_per_minute=max(5, 2 * len(symbols)),
                               max_abs_slippage_bps=KILL_MAX_ABS_SLIPPAGE_BPS,
                               max_price_move_frac=KILL_MAX_PRICE_MOVE_FRAC,
                               daily_loss_cap_usd=KILL_DAILY_LOSS_CAP_USD,
                               min_request_confidence=KILL_MIN_REQUEST_CONFIDENCE),
        exit_params=ExitParams(stop_pct=STOP_PCT, target_pct=TARGET_PCT),
        daily_loss_budget_usd=DAILY_LOSS_BUDGET_USD,
        costs=CostModel(fee_rate=FEE_RATE, spread_bps=PAPER_SPREAD_BPS, slippage_bps=PAPER_SLIPPAGE_BPS))
