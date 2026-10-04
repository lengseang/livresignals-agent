"""Policy engine (Contract E) and the policy -> TradeProposal step (Contract F).

Pure and deterministic. Configured thresholds, never inferred. Policy can only DEMOTE:
every rule lowers the confidence the ladder sees, mirroring the risk layer's
"modifiers may only reduce risk". Demotions are returned as reasons so the decision
log can say WHY a 0.95-confidence decision did not execute.

Demotion rules (all configurable via PolicyConfig):
  * source == "fallback"            -> never EXECUTE (capped to STRATEGY_CHECK)
  * action HOLD / NO_TRADE          -> IGNORE (nothing to act on, however confident)
  * entry_quality < min_entry_quality, would-be EXECUTE -> STRATEGY_CHECK
  * risk_level in blocked_risk_levels                   -> WATCH
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from trading.v2.contracts import (
    Decision, DecisionState, PolicyDecision, Thresholds, TradeProposal,
    tier_for_confidence,
)
from trading.v2.ids import derive_id

ESCALATION_TARGET = "hermes"


def _just_below(x: float) -> float:
    return math.nextafter(x, 0.0)


@dataclass(frozen=True)
class PolicyConfig:
    policy_version: str = "2.0.0"
    thresholds: Thresholds = Thresholds(
        ignore_below=0.55, watch_below=0.75, strategy_check_below=0.90, execute_at_or_above=0.90
    )
    min_entry_quality: float = 0.70
    blocked_risk_levels: tuple[str, ...] = ("EXTREME",)


@dataclass(frozen=True)
class PolicyResult:
    policy: PolicyDecision
    reasons: tuple[str, ...]  # why confidence was demoted; empty if it was not


def evaluate_policy(decision: Decision, cfg: PolicyConfig = PolicyConfig()) -> PolicyResult:
    t = cfg.thresholds
    adjusted = decision.confidence
    reasons: list[str] = []

    def cap(ceiling: float, why: str) -> None:
        nonlocal adjusted
        if adjusted > ceiling:
            adjusted = ceiling
            reasons.append(why)

    if decision.source == "fallback":
        cap(_just_below(t.execute_at_or_above), "fallback_capped_below_execute")
    if decision.action in ("HOLD", "NO_TRADE"):
        cap(_just_below(t.ignore_below), f"non_actionable_{decision.action.lower()}")
    if decision.signals is not None and decision.signals.entry_quality < cfg.min_entry_quality:
        cap(_just_below(t.execute_at_or_above),
            f"entry_quality_{decision.signals.entry_quality:.2f}_below_{cfg.min_entry_quality:.2f}")
    if decision.risk_level in cfg.blocked_risk_levels:
        cap(_just_below(t.watch_below), f"risk_level_{decision.risk_level.lower()}")

    tier = tier_for_confidence(adjusted, t)
    policy = PolicyDecision(
        contract="policy_decision", version="1.0", decision_id=decision.decision_id,
        policy_version=cfg.policy_version, tier=tier, thresholds_applied=t,
        adjusted_confidence=adjusted,
        escalate_to=ESCALATION_TARGET if tier == "STRATEGY_CHECK" else None,
        action=tier,
    )
    return PolicyResult(policy=policy, reasons=tuple(reasons))


@dataclass(frozen=True)
class ExitParams:
    """Calibrated stop/target fractions (data/exit-params.json via exit_calibrate.py).
    Injected, never defaulted: an unknown stop is not invented."""
    stop_pct: float
    target_pct: float


@dataclass(frozen=True)
class ProposalResult:
    proposal: Optional[TradeProposal]
    reason: str  # "proposed" or why no proposal


def propose_trade(
    decision: Decision,
    result: PolicyResult,
    state: DecisionState,
    exit_params: Optional[ExitParams],
) -> ProposalResult:
    """EXECUTE-tier decision -> a size-less TradeProposal, or None with the reason.

    One position at a time per symbol: an opposite-side signal while positioned is NOT a
    reversal, it is None ("exit first"); a duplicate same-side signal is None.
    """
    pol = result.policy
    if pol.decision_id != decision.decision_id or decision.state_id != state.state_id:
        return ProposalResult(None, "mismatched_decision_policy_state")
    if pol.tier != "EXECUTE":
        return ProposalResult(None, f"tier_{pol.tier.lower()}")
    pos = state.position.side
    action = decision.action
    if action in ("HOLD", "NO_TRADE"):
        return ProposalResult(None, "non_actionable")
    if action in ("LONG", "SHORT"):
        want = action.lower()
        if pos == want:
            return ProposalResult(None, f"already_{want}")
        if pos != "none":
            return ProposalResult(None, "opposite_position_exit_first")
        if exit_params is None:
            return ProposalResult(None, "exit_params_unknown")
        intent, side = "entry", action
        stop, target = exit_params.stop_pct, exit_params.target_pct
    else:  # EXIT / REDUCE
        if pos == "none":
            return ProposalResult(None, "no_position_to_reduce")
        intent, side = ("exit" if action == "EXIT" else "reduce"), pos.upper()
        stop = target = None
    proposal = TradeProposal(
        contract="trade_proposal", version="1.0",
        proposal_id=derive_id("tp", decision.decision_id, pol.policy_version),
        symbol=state.symbol, side=side, strategy=state.strategy, intent=intent,
        reference_price=state.market.price, stop_pct=stop, target_pct=target,
        rationale={"decision_id": decision.decision_id, "policy_tier": pol.tier},
    )
    return ProposalResult(proposal, "proposed")
