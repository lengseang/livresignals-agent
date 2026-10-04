"""State builder (Contract C): features + position + risk + strategy -> DecisionState.

Assembled by OUR software only. A snapshot that is incomplete or stale is refused here, loudly
(UnusableSnapshot), not turned into a weak signal. The state carries no credentials by construction.
"""
from __future__ import annotations

from datetime import datetime

from trading.v2.contracts import (
    DecisionState, FeatureSnapshot, PositionState, RiskState, StrategyRef,
)
from trading.v2.ids import derive_id


def build_decision_state(snapshot: FeatureSnapshot, *, price: float, spread_bps: float,
                         position: PositionState, risk: RiskState, strategy: StrategyRef,
                         max_stale_ms: int) -> DecisionState:
    snapshot.require_usable(max_stale_ms)     # raises UnusableSnapshot
    as_of = datetime.fromisoformat(snapshot.as_of_utc[:-1] + "+00:00")
    ts = int(as_of.timestamp())
    return DecisionState(
        contract="decision_state", version="1.0",
        state_id=derive_id("st", snapshot.symbol, snapshot.snapshot_id, strategy.name, strategy.version),
        timestamp=ts, symbol=snapshot.symbol,
        market={"price": price, "spread_bps": spread_bps},
        features=snapshot.snapshot_id, regime=snapshot.regime,
        position=position, risk=risk, strategy=strategy,
    )
