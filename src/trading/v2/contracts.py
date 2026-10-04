"""V2 service contracts A-I as pure, strict Pydantic models (Stage 1).

Spec: docs/v2/contracts.md, amended by the owner's decisions after comparing it
with the original architecture note ("HER"): see docs/v2/STAGE1-DECISIONS.md.

Design rules enforced here:
  * No I/O, no network, no logging, no clock. Validation and serialisation only.
  * STRICT: malformed input is rejected, never coerced ("high" is not 0.0).
  * extra="forbid": unknown fields are errors. Contract F therefore cannot carry
    a size, and Contract C cannot smuggle credentials.
  * Immutable (frozen).
  * Position size exists only in Contract G (computed) and H (copied from an
    approved G). It is never present on a proposal or a model output.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Annotated, Literal, Optional

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

# ---------------------------------------------------------------- primitives

_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


def _check_utc(value: str) -> str:
    if not _UTC_RE.match(value):
        raise ValueError(f"not an ISO-8601 UTC timestamp ending in 'Z': {value!r}")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:  # e.g. month 13
        raise ValueError(f"invalid calendar timestamp: {value!r}") from exc
    return value


UtcTime = Annotated[str, AfterValidator(_check_utc)]
Symbol = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{5,20}$")]
NonEmpty = Annotated[str, StringConstraints(min_length=1)]
Pos = Annotated[float, Field(gt=0)]
NonNeg = Annotated[float, Field(ge=0)]
Unit = Annotated[float, Field(ge=0, le=1)]  # probability / score in [0, 1]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
VersionTag = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
Timeframe = Annotated[str, StringConstraints(pattern=r"^\d+[mhdw]$")]
StateHash = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


def _id(prefix: str):
    return Annotated[str, StringConstraints(pattern=rf"^{prefix}_[A-Za-z0-9]+$")]


MarketEventId = _id("mkt")
SnapshotId = _id("feat")
StateId = _id("st")
DecisionId = _id("dec")
TradeProposalId = _id("tp")
RiskDecisionId = _id("rd")
CommandId = _id("ex")
HermesProposalId = _id("prop")
HaltRequestId = _id("hr")
AdjustmentId = _id("padj")

Venue = Literal["binance", "bybit"]
Tier = Literal["IGNORE", "WATCH", "STRATEGY_CHECK", "EXECUTE"]
Action = Literal["LONG", "SHORT", "HOLD", "EXIT", "REDUCE", "NO_TRADE"]
RiskLevel = Literal["LOW", "MEDIUM", "HIGH", "EXTREME"]
FALLBACK_STRATEGIES = ("trend_follower", "donchian_ensemble", "tsmom", "ctrend")
# V2-15: the System One gateway answers the SAME questions as Jev when Jev is down. It is not a voter.
GATEWAY_STRATEGY = "system_one_gateway"
FallbackStrategy = Literal["trend_follower", "donchian_ensemble", "tsmom", "ctrend", "system_one_gateway"]

# A fallback decision's confidence must stay strictly below this. It equals the
# policy's execute_at_or_above (0.90): defence in depth so a fallback can never
# reach the EXECUTE tier even if a policy bug forgets to cap it.
FALLBACK_CONFIDENCE_CEILING = 0.90


class _Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False
    )

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_dict(cls, data: dict):
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, text: str):
        return cls.model_validate_json(text)


# ---------------------------------------------------------------- Contract A


class MarketEvent(_Contract):
    contract: Literal["market_event"]
    version: Literal["1.0"]
    event_id: MarketEventId
    symbol: Symbol
    venue: Venue
    event_time_utc: UtcTime
    received_time_utc: UtcTime
    source: Literal["ws", "rest", "ingest_all.py"]
    kind: Literal["trade", "book", "kline_closed"]
    price: Optional[Pos] = None
    size: Optional[Pos] = None
    bid: Optional[Pos] = None
    ask: Optional[Pos] = None
    spread_bps: Optional[NonNeg] = None
    # Additions (owner-approved gap fill): which bar a kline_closed event closes,
    # and a per-stream sequence number for ordering / de-duplication on replay.
    timeframe: Optional[Timeframe] = None
    sequence: Optional[Annotated[int, Field(ge=0)]] = None

    @model_validator(mode="after")
    def _kind_fields(self):
        need = {
            "trade": ("price", "size"),
            "book": ("bid", "ask"),
            "kline_closed": ("price", "timeframe"),
        }[self.kind]
        missing = [f for f in need if getattr(self, f) is None]
        if missing:
            raise ValueError(f"{self.kind} event requires {missing}")
        if self.kind != "kline_closed" and self.timeframe is not None:
            raise ValueError("timeframe is only valid on kline_closed events")
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError(f"crossed book: bid {self.bid} > ask {self.ask}")
        return self

    @property
    def feed_latency_ms(self) -> float:
        """received - event time. May be negative (clock skew); never rejected."""
        t = lambda s: datetime.fromisoformat(s[:-1] + "+00:00")  # noqa: E731
        return (t(self.received_time_utc) - t(self.event_time_utc)).total_seconds() * 1000


# ---------------------------------------------------------------- Contract B

RETURN_HORIZONS = frozenset({"1m", "5m", "15m", "1h", "4h"})


class Technical(_Contract):
    rsi: Annotated[float, Field(ge=0, le=100)]
    atr: NonNeg
    ema_fast_slope: float
    ema_slow_slope: float
    distance_vwap: float


class VolumeFeatures(_Contract):
    volume_1m: NonNeg
    volume_zscore: float


class OrderbookFeatures(_Contract):
    imbalance: Annotated[float, Field(ge=-1, le=1)]
    bid_depth: NonNeg
    ask_depth: NonNeg


class Regime(_Contract):
    trend: Slug
    volatility: Slug
    liquidity: Slug


class DataQuality(_Contract):
    complete: bool
    missing_fields: list[NonEmpty]
    stale_ms: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _consistent(self):
        if self.complete and self.missing_fields:
            raise ValueError("complete=true but missing_fields is not empty")
        if not self.complete and not self.missing_fields:
            raise ValueError("complete=false requires the missing fields to be named")
        return self


class UnusableSnapshot(ValueError):
    """Raised when a snapshot is incomplete or stale. Never a 'weak signal'."""


class FeatureSnapshot(_Contract):
    contract: Literal["feature_snapshot"]
    version: Literal["1.0"]
    snapshot_id: SnapshotId
    # Addition (owner-approved): the feature-definition version, so a changed RSI
    # definition cannot silently invalidate backtests.
    feature_version: VersionTag
    symbol: Symbol
    as_of_utc: UtcTime
    returns: dict[str, float]
    technical: Technical
    volume: VolumeFeatures
    orderbook: OrderbookFeatures
    regime: Regime
    data_quality: DataQuality

    @model_validator(mode="after")
    def _returns_keys(self):
        if set(self.returns) != RETURN_HORIZONS:
            raise ValueError(
                f"returns must have exactly {sorted(RETURN_HORIZONS)}, got {sorted(self.returns)}"
            )
        return self

    def require_usable(self, max_stale_ms: int) -> None:
        if not self.data_quality.complete:
            raise UnusableSnapshot(
                f"incomplete snapshot, missing {self.data_quality.missing_fields}"
            )
        if self.data_quality.stale_ms > max_stale_ms:
            raise UnusableSnapshot(
                f"stale snapshot: {self.data_quality.stale_ms}ms > ceiling {max_stale_ms}ms"
            )


# ---------------------------------------------------------------- Contract C


class MarketState(_Contract):
    price: Pos
    spread_bps: NonNeg


class PositionState(_Contract):
    side: Literal["none", "long", "short"]
    size: NonNeg
    entry_price: Optional[Pos] = None
    unrealized_pnl: float

    @model_validator(mode="after")
    def _flat_or_open(self):
        if self.side == "none":
            if self.size != 0 or self.entry_price is not None:
                raise ValueError("side=none requires size 0 and entry_price null")
        elif self.size <= 0 or self.entry_price is None:
            raise ValueError(f"side={self.side} requires size > 0 and an entry_price")
        return self


class RiskState(_Contract):
    equity: Pos
    daily_pnl: float
    daily_loss_limit_remaining: NonNeg
    current_exposure: NonNeg


class StrategyRef(_Contract):
    name: NonEmpty
    version: NonEmpty


class DecisionState(_Contract):
    """The complete input to a decision. No credentials, accounts or endpoints:
    extra="forbid" means any such field is a validation error."""

    contract: Literal["decision_state"]
    version: Literal["1.0"]
    state_id: StateId
    timestamp: Annotated[int, Field(gt=0)]
    symbol: Symbol
    market: MarketState
    features: SnapshotId  # Contract B by reference (snapshot_id)
    regime: Regime
    position: PositionState
    risk: RiskState
    strategy: StrategyRef

    def state_hash(self) -> str:
        """Deterministic content hash for replay: sha256 of canonical JSON."""
        canon = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canon.encode()).hexdigest()


# ---------------------------------------------------------------- Contract D


class QuestionAnswer(_Contract):
    answer: NonEmpty
    probability: Unit


class Questions(_Contract):
    market_regime: QuestionAnswer
    trend_quality: QuestionAnswer
    entry_quality: QuestionAnswer
    breakout_probability: QuestionAnswer
    reversal_probability: QuestionAnswer
    risk_level: QuestionAnswer


class ActionProbabilities(_Contract):
    LONG: Unit
    SHORT: Unit
    HOLD: Unit
    EXIT: Unit
    REDUCE: Unit
    NO_TRADE: Unit


class Signals(_Contract):
    trend_quality: Unit
    entry_quality: Unit
    expected_edge: Unit
    timing_quality: Unit


class Decision(_Contract):
    contract: Literal["decision"]
    version: Literal["1.0"]
    decision_id: DecisionId
    state_id: StateId
    state_hash: StateHash
    source: Literal["jev", "fallback"]
    fallback_reason: Optional[NonEmpty] = None
    fallback_strategy: Optional[FallbackStrategy] = None
    model: Optional[NonEmpty] = None
    workflow_version: Optional[VersionTag] = None
    action: Action
    confidence: Unit
    probabilities: Optional[ActionProbabilities] = None
    signals: Optional[Signals] = None
    risk_level: Optional[RiskLevel] = None
    questions: Optional[Questions] = None
    latency_ms: NonNeg

    @model_validator(mode="after")
    def _source_rules(self):
        jev_only = (
            "model", "workflow_version", "probabilities", "signals",
            "risk_level", "questions",
        )
        if self.source == "jev":
            missing = [f for f in jev_only if getattr(self, f) is None]
            if missing:
                raise ValueError(f"jev decision requires {missing}")
            if self.fallback_reason is not None or self.fallback_strategy is not None:
                raise ValueError("jev decision must not carry fallback_reason/strategy")
        else:
            if not self.fallback_reason:
                raise ValueError("fallback decision requires a fallback_reason")
            if self.fallback_strategy is None:
                raise ValueError(
                    f"fallback decision requires fallback_strategy in {FALLBACK_STRATEGIES + (GATEWAY_STRATEGY,)}"
                )
            if self.fallback_strategy == GATEWAY_STRATEGY:
                # the gateway travels the same road as Jev: every answer present, so the policy's demotions
                # (entry_quality, risk_level) apply to it exactly as they do to Jev
                missing = [f for f in jev_only if getattr(self, f) is None]
                if missing:
                    raise ValueError(f"gateway decision requires {missing}")
            else:
                present = [f for f in jev_only if getattr(self, f) is not None]
                if present:
                    raise ValueError(f"fallback decision must not carry Jev fields {present}")
            if self.confidence >= FALLBACK_CONFIDENCE_CEILING:
                raise ValueError(
                    f"fallback confidence {self.confidence} must be < "
                    f"{FALLBACK_CONFIDENCE_CEILING} (fallback can never reach EXECUTE)"
                )
        return self


# ---------------------------------------------------------------- Contract E


class Thresholds(_Contract):
    ignore_below: float
    watch_below: float
    strategy_check_below: float
    execute_at_or_above: float

    @model_validator(mode="after")
    def _ordered(self):
        if not (
            0 < self.ignore_below < self.watch_below < self.strategy_check_below <= 1
        ):
            raise ValueError("thresholds must satisfy 0 < ignore < watch < check <= 1")
        if self.strategy_check_below != self.execute_at_or_above:
            raise ValueError(
                "strategy_check_below must equal execute_at_or_above (no gap/overlap)"
            )
        return self


def tier_for_confidence(confidence: float, t: Thresholds) -> str:
    """Each threshold is a lower bound of the tier above it (0.90 -> EXECUTE)."""
    if confidence < t.ignore_below:
        return "IGNORE"
    if confidence < t.watch_below:
        return "WATCH"
    if confidence < t.strategy_check_below:
        return "STRATEGY_CHECK"
    return "EXECUTE"


class PolicyDecision(_Contract):
    contract: Literal["policy_decision"]
    version: Literal["1.0"]
    decision_id: DecisionId
    policy_version: VersionTag
    tier: Tier
    thresholds_applied: Thresholds
    adjusted_confidence: Unit
    escalate_to: Optional[NonEmpty] = None
    action: Tier

    @model_validator(mode="after")
    def _consistent(self):
        expected = tier_for_confidence(self.adjusted_confidence, self.thresholds_applied)
        if self.tier != expected:
            raise ValueError(
                f"tier {self.tier} inconsistent with adjusted_confidence "
                f"{self.adjusted_confidence} (expected {expected})"
            )
        if self.action != self.tier:
            raise ValueError("action must equal tier")
        if self.escalate_to is not None and self.tier != "STRATEGY_CHECK":
            raise ValueError("escalate_to is only valid for tier STRATEGY_CHECK")
        return self


# ---------------------------------------------------------------- Contract F


class Rationale(_Contract):
    decision_id: DecisionId
    policy_tier: Tier


class TradeProposal(_Contract):
    """No size field, by design. extra="forbid" rejects any attempt to add one."""

    contract: Literal["trade_proposal"]
    version: Literal["1.0"]
    proposal_id: TradeProposalId
    symbol: Symbol
    # For entry: the side to open. For exit/reduce/flatten: the side of the
    # EXISTING position being reduced.
    side: Literal["LONG", "SHORT"]
    strategy: StrategyRef
    intent: Literal["entry", "exit", "reduce", "flatten"]
    reference_price: Pos
    stop_pct: Optional[Annotated[float, Field(gt=0, lt=1)]] = None
    target_pct: Optional[Annotated[float, Field(gt=0)]] = None
    rationale: Rationale

    @model_validator(mode="after")
    def _intent_rules(self):
        if self.intent == "entry":
            if self.stop_pct is None or self.target_pct is None:
                raise ValueError("entry requires stop_pct and target_pct")
            if self.rationale.policy_tier != "EXECUTE":
                raise ValueError("an entry proposal requires policy_tier EXECUTE")
        elif self.stop_pct is not None or self.target_pct is not None:
            raise ValueError(f"{self.intent} must not carry stop_pct/target_pct")
        return self


# ---------------------------------------------------------------- Contract G

REQUIRED_RISK_CHECKS = (
    "daily_loss_limit",
    "max_position_size",
    "max_leverage",
    "spread_ceiling",
    "duplicate_order",
    "exchange_health",
    "kill_switch",
)


class RiskCheck(_Contract):
    name: Slug
    passed: bool
    detail: NonEmpty


class RiskDecision(_Contract):
    """Deterministic. Extra checks (e.g. the existing engine's weekly/drawdown
    halts, exposure cap, stop distance) are allowed; the seven spec checks are
    mandatory and every check, passed or failed, must be listed."""

    contract: Literal["risk_decision"]
    version: Literal["1.0"]
    risk_decision_id: RiskDecisionId  # needed so Contract H can reference it
    risk_version: VersionTag
    proposal_id: TradeProposalId
    approved: bool
    checks: list[RiskCheck]
    size: Optional[Pos] = None
    stop_price: Optional[Pos] = None
    # Notion's Risk Decision carries max_slippage_bps; contracts.md dropped it. Without it a gap between the
    # decision (reference price) and the fill silently inflates risk and can make an order unfundable.
    max_slippage_bps: Optional[Pos] = None
    # V2-15: the take-profit level. propose_trade always computed target_pct, but nothing downstream carried it, so
    # no position could ever take profit: a near-target trade could only end at its stop or on a Jev EXIT.
    target_price: Optional[Pos] = None
    reject_reason: Optional[NonEmpty] = None

    @model_validator(mode="after")
    def _rules(self):
        names = [c.name for c in self.checks]
        if len(names) != len(set(names)):
            raise ValueError("duplicate check names")
        missing = [n for n in REQUIRED_RISK_CHECKS if n not in names]
        if missing:
            raise ValueError(f"missing required checks {missing}")
        failed = [c.name for c in self.checks if not c.passed]
        if self.approved:
            if failed:
                raise ValueError(f"approved but checks failed: {failed}")
            if self.size is None:
                raise ValueError("approved decision must carry a computed size")
            if self.reject_reason is not None:
                raise ValueError("approved decision must not carry reject_reason")
        else:
            if not failed:
                raise ValueError("rejected decision must have at least one failed check")
            if self.reject_reason not in failed:
                raise ValueError("reject_reason must name a failed check")
            if (self.size is not None or self.stop_price is not None or self.max_slippage_bps is not None
                    or self.target_price is not None):
                raise ValueError("rejected decision must not carry size, stop_price, max_slippage_bps or target_price")
        if self.target_price is not None and self.stop_price is None:
            raise ValueError("target_price without a stop_price: a target is only valid on a protected entry")
        return self


# ---------------------------------------------------------------- Contract H


class ExecutionRefused(Exception):
    """Execution refuses to act without an approved, matching risk decision."""


class ExecutionCommand(_Contract):
    contract: Literal["execution_command"]
    version: Literal["1.0"]
    command_id: CommandId
    proposal_id: TradeProposalId
    risk_decision_id: RiskDecisionId
    venue: Venue
    symbol: Symbol
    side: Literal["BUY", "SELL"]
    type: Literal["MARKET", "LIMIT"]
    limit_price: Optional[Pos] = None  # addition: a LIMIT order is unplaceable without it
    size: Pos
    stop_price: Optional[Pos] = None  # addition (owner-approved): protective stop
    # Worst acceptable fill price for an OPENING order (buy: fill must be <= it; sell: >= it): the order's
    # slippage protection, derived from the reference price and the risk decision's max_slippage_bps.
    worst_price: Optional[Pos] = None
    target_price: Optional[Pos] = None  # V2-15: take-profit level (opening orders only); from the risk decision
    position_side: Literal["LONG", "SHORT"]  # Binance hedge mode: required
    reduce_only: bool
    idempotency_key: NonEmpty

    @model_validator(mode="after")
    def _rules(self):
        if (self.type == "LIMIT") != (self.limit_price is not None):
            raise ValueError("limit_price is required for LIMIT and forbidden for MARKET")
        # Hedge mode: opening BUY->LONG, SELL->SHORT; reducing BUY->SHORT, SELL->LONG.
        opening = not self.reduce_only
        expected = (
            ("LONG" if self.side == "BUY" else "SHORT")
            if opening
            else ("SHORT" if self.side == "BUY" else "LONG")
        )
        if self.position_side != expected:
            raise ValueError(
                f"{self.side} reduce_only={self.reduce_only} requires "
                f"position_side {expected}, got {self.position_side}"
            )
        if opening and self.stop_price is None:
            raise ValueError("an opening order requires a protective stop_price")
        if not opening and self.stop_price is not None:
            raise ValueError("a reduce-only order must not carry stop_price")
        if opening and self.worst_price is None:
            raise ValueError("an opening order requires worst_price (slippage protection)")
        if not opening and self.worst_price is not None:
            raise ValueError("a reduce-only order must not carry worst_price: exits must not be refused for price")
        if not opening and self.target_price is not None:
            raise ValueError("a reduce-only order must not carry target_price")
        if opening and self.target_price is not None:
            long = self.position_side == "LONG"
            if (long and not self.target_price > self.stop_price) or (not long and not self.target_price < self.stop_price):
                raise ValueError("target_price must be on the profit side of stop_price")
        if not re.fullmatch(rf"{re.escape(self.proposal_id)}-v\d+", self.idempotency_key):
            raise ValueError("idempotency_key must be '<proposal_id>-v<n>'")
        return self

    @classmethod
    def from_approved(
        cls,
        *,
        proposal: TradeProposal,
        risk_decision: RiskDecision,
        command_id: str,
        venue: str,
        order_type: str = "MARKET",
        limit_price: Optional[float] = None,
        attempt: int = 1,
    ) -> "ExecutionCommand":
        """The only sanctioned constructor: size and stop come from the approved
        risk decision, never from the proposal."""
        if not risk_decision.approved:
            raise ExecutionRefused(
                f"risk decision {risk_decision.risk_decision_id} is not approved"
            )
        if risk_decision.proposal_id != proposal.proposal_id:
            raise ExecutionRefused("risk decision is for a different proposal")
        if risk_decision.size is None:  # unreachable for a valid approved decision
            raise ExecutionRefused("approved risk decision carries no size")
        entry = proposal.intent == "entry"
        if entry and risk_decision.stop_price is None:
            raise ExecutionRefused("entry approved without a protective stop_price")
        if entry and risk_decision.max_slippage_bps is None:
            raise ExecutionRefused("entry approved without max_slippage_bps (slippage protection)")
        if entry and risk_decision.target_price is None:
            raise ExecutionRefused("entry approved without a target_price: the position could never take profit")
        if entry:
            side = "BUY" if proposal.side == "LONG" else "SELL"
        else:
            side = "SELL" if proposal.side == "LONG" else "BUY"
        return cls(
            contract="execution_command",
            version="1.0",
            command_id=command_id,
            proposal_id=proposal.proposal_id,
            risk_decision_id=risk_decision.risk_decision_id,
            venue=venue,
            symbol=proposal.symbol,
            side=side,
            type=order_type,
            limit_price=limit_price,
            size=risk_decision.size,
            stop_price=risk_decision.stop_price if entry else None,
            target_price=risk_decision.target_price if entry else None,
            worst_price=(proposal.reference_price * (1 + risk_decision.max_slippage_bps / 1e4 if side == "BUY"
                                                      else 1 - risk_decision.max_slippage_bps / 1e4)
                         if entry else None),
            position_side=proposal.side,
            reduce_only=not entry,
            idempotency_key=f"{proposal.proposal_id}-v{attempt}",
        )


# ---------------------------------------------------------------- Contract I

HermesProposalType = Literal[
    "NEW_STRATEGY", "MODIFY_STRATEGY", "CHANGE_THRESHOLD", "CHANGE_JEV_WORKFLOW",
    "CHANGE_FEATURE", "CHANGE_POSITION_SIZING", "DISABLE_STRATEGY", "ENABLE_STRATEGY",
]

# Hermes may never change these (guide §2 / permissions table). Defence in depth:
# a proposal whose change targets one is invalid at the contract level.
FORBIDDEN_CHANGE_KEY_FRAGMENTS = (
    "leverage", "kill_switch", "risk_limit", "max_risk", "daily_loss", "max_position",
    "max_drawdown", "live_enabled", "api_key", "secret", "withdraw",
)


class Evidence(_Contract):
    backtest_trades: Annotated[int, Field(ge=1)]
    sharpe_delta: float
    max_drawdown_delta: float


class HermesProposal(_Contract):
    contract: Literal["hermes_proposal"]
    version: Literal["1.0"]
    proposal_id: HermesProposalId
    type: HermesProposalType
    target: NonEmpty
    change: dict[str, JsonValue]
    evidence: Evidence
    status: Literal["proposed"]  # inert: only 'proposed' is representable here

    @model_validator(mode="after")
    def _change_rules(self):
        if not self.change:
            raise ValueError("change must not be empty")
        for key in self.change:
            low = key.lower()
            hit = [f for f in FORBIDDEN_CHANGE_KEY_FRAGMENTS if f in low]
            if hit:
                raise ValueError(
                    f"change key {key!r} touches a Hermes-forbidden area ({hit[0]})"
                )
        return self


# ---------------------------------------------------------------- Contract J (from the Notion note, Level 5)

HALT_ACTIONS = ("HALT_ALL", "HALT_SYMBOL", "HALT_STRATEGY", "REDUCE_EXPOSURE")


class HaltRequest(_Contract):
    """Hermes may REQUEST a halt; it can never perform one. The deterministic controller decides."""

    contract: Literal["halt_request"]
    version: Literal["1.0"]
    request_id: HaltRequestId
    requested_by: Literal["hermes"]
    action: Literal["HALT_ALL", "HALT_SYMBOL", "HALT_STRATEGY", "REDUCE_EXPOSURE"]
    target: Optional[NonEmpty] = None
    reason: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    confidence: Unit

    @model_validator(mode="after")
    def _scope(self):
        if self.action == "HALT_ALL" and self.target is not None:
            raise ValueError("HALT_ALL takes no target")
        if self.action in ("HALT_SYMBOL", "HALT_STRATEGY") and self.target is None:
            raise ValueError(f"{self.action} requires a target")
        if self.action == "HALT_SYMBOL" and not re.fullmatch(r"[A-Z0-9]{5,20}", self.target):
            raise ValueError("HALT_SYMBOL target must be a symbol like BTCUSDT")
        return self


# ---------------------------------------------------------------- Contract K (V2-15)


class PositionAdjustment(_Contract):
    """A protective-stop amendment on an OPEN position. It can only TIGHTEN (move toward profit): a long's stop
    only rises, a short's only falls, and never to or beyond the last price (that would be an exit disguised as a
    stop). It is not a new order, so the kill switch does not block it: it is how a position stays protected
    while everything else is halted."""

    contract: Literal["position_adjustment"]
    version: Literal["1.0"]
    adjustment_id: AdjustmentId
    symbol: Symbol
    position_side: Literal["LONG", "SHORT"]
    kind: Literal["TIGHTEN_STOP"]
    old_stop: Pos
    new_stop: Pos
    last_price: Pos
    source: Literal["ratchet", "jev", "gateway"]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    ts_ms: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def _tightens(self):
        if self.position_side == "LONG":
            if not self.new_stop > self.old_stop:
                raise ValueError("a LONG stop can only rise")
            if not self.new_stop < self.last_price:
                raise ValueError("a LONG stop must stay below the last price")
        else:
            if not self.new_stop < self.old_stop:
                raise ValueError("a SHORT stop can only fall")
            if not self.new_stop > self.last_price:
                raise ValueError("a SHORT stop must stay above the last price")
        return self


ALL_CONTRACTS = {
    "market_event": MarketEvent,
    "feature_snapshot": FeatureSnapshot,
    "decision_state": DecisionState,
    "decision": Decision,
    "policy_decision": PolicyDecision,
    "trade_proposal": TradeProposal,
    "risk_decision": RiskDecision,
    "execution_command": ExecutionCommand,
    "hermes_proposal": HermesProposal,
    "halt_request": HaltRequest,
    "position_adjustment": PositionAdjustment,
}
