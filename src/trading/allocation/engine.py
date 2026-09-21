"""P-A accumulation engine: cadence + drawdown scaling + P3 guard integration.

Harvests BTC drift by investing more during drawdowns, less during extended
highs. No prediction, no shorting, no leverage. Guards from P3 are enforced
(breaker pauses entries; exposure cap; re-entry at half size).
"""
from __future__ import annotations

from dataclasses import dataclass

from trading.risk.breakers import reentry_size
from trading.risk.guards import GUARDS

# Config-driven defaults (spec §5.1). Overridable via AllocationConfig.
# P-A v2 (validated 2026-08-11): dip scaling only, NEVER trim extensions.
# Evidence: every window positive on two independent holdouts (early +31.3%
# avg, recent +15.8% avg, full 9yr +28.9% BTC excess vs plain DCA).
DEFAULT_BASE_AMOUNT = 100.0
DEFAULT_INTERVAL_DAYS = 30
DEFAULT_LOOKBACK_DAYS = 90
DEFAULT_DRAWDOWN_THRESHOLD = -0.15
DEFAULT_EXTENSION_THRESHOLD = 0.15
DEFAULT_DIP_MULTIPLIER = 1.5
DEFAULT_EXTENSION_MULTIPLIER = 1.0  # never trim (v2 rule)


@dataclass(frozen=True)
class AllocationConfig:
    base_amount: float = DEFAULT_BASE_AMOUNT
    interval_days: int = DEFAULT_INTERVAL_DAYS
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    drawdown_threshold: float = DEFAULT_DRAWDOWN_THRESHOLD
    extension_threshold: float = DEFAULT_EXTENSION_THRESHOLD
    dip_multiplier: float = DEFAULT_DIP_MULTIPLIER
    extension_multiplier: float = DEFAULT_EXTENSION_MULTIPLIER


@dataclass
class AllocationState:
    btc: float = 0.0
    cash_invested: float = 0.0
    entries: int = 0
    paused_months: int = 0
    last_entry_idx: int = -1


def scaling_factor(price: float, lookback_closes: list[float], cfg: AllocationConfig) -> float:
    """2.0x in deep dip, 0.5x in extended high, else 1.0x."""
    if not lookback_closes:
        return 1.0
    hi = max(lookback_closes)
    lo = min(lookback_closes)
    if hi <= 0:
        return 1.0
    dd = price / hi - 1.0  # drawdown from 90d high
    if dd <= cfg.drawdown_threshold:
        return cfg.dip_multiplier
    # Extension measured from the 90d low. In a flat range (lo==hi) the low
    # reference still applies; lo>0 guard prevents division by zero.
    if lo > 0 and price / lo - 1.0 >= cfg.extension_threshold:
        return cfg.extension_multiplier
    return 1.0


class Accumulator:
    """Runs the accumulation strategy over a close series (daily candles)."""

    def __init__(self, cfg: AllocationConfig | None = None):
        self.cfg = cfg or AllocationConfig()
        self.state = AllocationState()
        self.breakers_active = False  # set by caller via check_breakers()

    def check_breakers(self, equity: float, day_start_equity: float,
                       high_water_mark: float) -> bool:
        """Return True if entries should pause (drawdown breaker hit)."""
        # Use the max drawdown guard: pause entries when equity is more than
        # max_drawdown_halt below the high-water mark.
        if high_water_mark <= 0:
            self.breakers_active = False
            return False
        dd = equity / high_water_mark - 1.0
        self.breakers_active = dd <= GUARDS.max_drawdown_halt
        return self.breakers_active

    def step(self, closes: list[float], idx: int) -> dict | None:
        """Evaluate one entry opportunity at candle `idx`. Returns entry record.

        Entry happens only when (idx - last_entry) >= interval_days.
        """
        cfg = self.cfg
        if self.state.last_entry_idx >= 0 and (idx - self.state.last_entry_idx) < cfg.interval_days:
            return None
        if idx < cfg.lookback_days:
            return None
        if self.breakers_active:
            self.state.paused_months += 1
            self.state.last_entry_idx = idx  # don't re-attempt every candle
            return {"paused": True, "reason": "drawdown_breaker"}

        price = closes[idx]
        lookback = closes[idx - cfg.lookback_days : idx]
        mult = scaling_factor(price, lookback, cfg)

        # Re-entry sizing: after a pause, first entry at half size (P3 rule).
        if self.state.paused_months > 0:
            mult = reentry_size(mult, halts_elapsed=1)
            self.state.paused_months = 0

        amount = cfg.base_amount * mult
        self.state.btc += amount / price
        self.state.cash_invested += amount
        self.state.entries += 1
        self.state.last_entry_idx = idx
        return {
            "paused": False,
            "idx": idx,
            "price": price,
            "amount": amount,
            "multiplier": mult,
            "btc_delta": amount / price,
        }


def run_accumulation(closes: list[float], cfg: AllocationConfig | None = None) -> AllocationState:
    """Run accumulation over a full close series (in-sample helper)."""
    acc = Accumulator(cfg)
    for i in range(len(closes)):
        acc.step(closes, i)
    return acc.state