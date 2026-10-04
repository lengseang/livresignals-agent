"""The deterministic fallback (Contract D): the four EXISTING validated strategies vote.

No new trading logic lives here. This module only AGGREGATES stances that
`trend_follower`, `donchian_ensemble`, `tsmom` and `ctrend` already produce, through a
`FallbackSource`. The aggregation is deliberately dull, because a clever fallback is a
second hot path:
  * confidence = (votes for the winning stance / 4), capped at FALLBACK_MAX_CONFIDENCE (0.89),
    so it can reach at most STRATEGY_CHECK (escalate to Hermes), never EXECUTE.
  * UNAVAILABLE strategies count in the denominator: missing votes lower confidence.
  * no strict winner -> no action (HOLD, or NO_TRADE if flat), confidence 0.0.
  * this function never raises on bad input from a source; see `FallbackDecider.decide`.
"""
from __future__ import annotations

from typing import Literal, Mapping, Protocol

from trading.v2.contracts import FALLBACK_STRATEGIES, DecisionState

Stance = Literal["LONG", "SHORT", "FLAT", "UNAVAILABLE"]
STANCES = ("LONG", "SHORT", "FLAT", "UNAVAILABLE")
FALLBACK_MAX_CONFIDENCE = 0.89  # must stay < contracts.FALLBACK_CONFIDENCE_CEILING (0.90)


class FallbackSource(Protocol):
    def stances(self, state: DecisionState) -> Mapping[str, str]:
        """Current stance of each fallback strategy for `state.symbol`."""


class FallbackVerdict:
    __slots__ = ("action", "confidence", "strategy", "reason", "votes")

    def __init__(self, action: str, confidence: float, strategy: str, reason: str, votes: dict):
        self.action, self.confidence, self.strategy = action, confidence, strategy
        self.reason, self.votes = reason, votes

    def __repr__(self) -> str:
        return f"FallbackVerdict({self.action}, {self.confidence}, {self.strategy}, {self.reason!r})"


def _action_for(winner: str, position: str) -> str:
    if winner == "LONG":
        return {"none": "LONG", "long": "HOLD", "short": "EXIT"}[position]
    if winner == "SHORT":
        return {"none": "SHORT", "short": "HOLD", "long": "EXIT"}[position]
    return "NO_TRADE" if position == "none" else "EXIT"  # winner FLAT


def aggregate(stances: Mapping[str, str], position: str) -> FallbackVerdict:
    """Pure aggregation of the four strategies' stances. Unknown/missing/invalid -> UNAVAILABLE."""
    clean = {}
    for name in FALLBACK_STRATEGIES:
        s = stances.get(name, "UNAVAILABLE")
        clean[name] = s if s in STANCES else "UNAVAILABLE"
    counts = {s: sum(1 for v in clean.values() if v == s) for s in ("LONG", "SHORT", "FLAT")}
    n_avail = sum(counts.values())
    idle = "HOLD" if position != "none" else "NO_TRADE"
    if n_avail == 0:
        return FallbackVerdict(idle, 0.0, FALLBACK_STRATEGIES[0], "no_strategy_available", clean)
    top = max(counts.values())
    winners = [s for s, c in counts.items() if c == top]
    if len(winners) > 1:
        return FallbackVerdict(idle, 0.0, FALLBACK_STRATEGIES[0], "no_consensus", clean)
    winner = winners[0]
    conf = min(top / len(FALLBACK_STRATEGIES), FALLBACK_MAX_CONFIDENCE)
    lead = next(n for n in FALLBACK_STRATEGIES if clean[n] == winner)
    return FallbackVerdict(_action_for(winner, position), conf, lead,
                           f"consensus_{winner.lower()}_{top}_of_{len(FALLBACK_STRATEGIES)}", clean)


class StaticSource:
    """A FallbackSource over fixed stances (tests, and a degraded mode where nothing is known)."""

    def __init__(self, stances: Mapping[str, str]):
        self._stances = dict(stances)

    def stances(self, state: DecisionState) -> Mapping[str, str]:
        return self._stances


# --------------------------------------------------------------------------------------------------
# Evidence gate. The spec calls the four fallback strategies "already OOS-validated". The repo's own
# results do not support that (docs/loop/results/P1-2026-09-27.md; docs/research/strategy-inventory.md):
VALIDATION_EVIDENCE = {
    "donchian_ensemble": "A1: FAIL the gate on deflated Sharpe (0.817 < 0.95), but OOS Sharpe 0.77, +27%, "
                         "max drawdown -9%, beats BTC hold and the basket",
    "ctrend": "A2 (CTREND-lite): FAIL badly: OOS Sharpe -0.02, -65% return, -81% max drawdown, negative before costs",
    "tsmom": "A3 (risk-managed TSMOM): FAIL: OOS Sharpe 0.29 vs BTC 0.42, loses to costs, -47% max drawdown",
    "trend_follower": "no gate result: 'SMA50 beat 100/150/200 OOS' is a code comment, not a recorded test",
}
# Default: only strategies that did NOT lose money out of sample may vote. Excluded ones count as
# UNAVAILABLE, which lowers confidence (missing votes count against). The owner can override (E9).
DEFAULT_ENABLED = ("trend_follower", "donchian_ensemble")


class GatedSource:
    """Wraps a FallbackSource: strategies outside `enabled` are forced to UNAVAILABLE, whatever they say."""

    def __init__(self, inner: FallbackSource, enabled: tuple[str, ...] = DEFAULT_ENABLED):
        unknown = [n for n in enabled if n not in FALLBACK_STRATEGIES]
        if unknown:
            raise ValueError(f"unknown fallback strategies {unknown}; known: {FALLBACK_STRATEGIES}")
        self.inner, self.enabled = inner, tuple(enabled)

    def stances(self, state: DecisionState) -> Mapping[str, str]:
        raw = self.inner.stances(state)
        return {n: (raw.get(n, "UNAVAILABLE") if n in self.enabled else "UNAVAILABLE")
                for n in FALLBACK_STRATEGIES}
