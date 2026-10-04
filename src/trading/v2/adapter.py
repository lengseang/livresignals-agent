"""The Jev adapter (Contract D): "a decision engine answered" vs "we decide ourselves".

evaluate(state) ALWAYS returns a valid Decision and never raises:
  1. ask the injected JevClient with a hard wall-clock timeout (own thread);
  2. validate the reply STRICTLY against our workflow: nothing is coerced
     ("high" for a score is a schema violation, not 0.0);
  3. on ANY failure, return the deterministic fallback, labelled source="fallback" with a reason.

Fallback categories (Contract D rule 2): timeout, http_error, malformed_json,
schema_violation, unknown_enum, balance_exhausted, plus client_error for anything unexpected.

The TypeSafe wire format is NOT known here (docs unreachable from the build sandbox): the
client returns our INTERNAL raw shape (RawJevResponse). Translating the live API to it is
V2-8. Reasons never include raw exception text (it could carry a URL or key): only a category,
the exception class name, and for validation errors the field paths.

Stats are kept so a run can PROVE it did work (an adapter that "passed" by doing nothing is
the failure this system already suffered once).
"""
from __future__ import annotations

import logging
import math
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Callable, Optional, Protocol, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from trading.v2.contracts import Decision, DecisionState, FeatureSnapshot
from trading.v2.fallback import FALLBACK_MAX_CONFIDENCE, FallbackSource, aggregate
from trading.v2.ids import derive_id
from trading.v2.workflow import ACTIONS, JevWorkflow

log = logging.getLogger("trading.v2.adapter")

CATEGORIES = ("timeout", "http_error", "malformed_json", "schema_violation", "unknown_enum",
              "balance_exhausted", "client_error")
DISTRIBUTION_TOLERANCE = 0.05  # action probabilities must sum to 1 within this


class JevError(Exception):
    """Base for failures a client may raise. `category` selects the fallback reason."""
    category = "client_error"


class JevHttpError(JevError):
    category = "http_error"


class JevMalformedJson(JevError):
    category = "malformed_json"


class JevBalanceExhausted(JevError):
    category = "balance_exhausted"


class JevProtocolError(Exception):
    """The reply parsed but violates our workflow (raised by normalisation)."""

    def __init__(self, category: str, detail: str):
        super().__init__(detail)
        self.category, self.detail = category, detail


class JevClient(Protocol):
    def evaluate(self, request: dict, timeout_s: float) -> object:
        """Return the parsed reply (internal raw shape). May raise JevError subclasses."""


# -------------------------------------------------- internal raw reply shape

_STRICT = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)

class RawAnswer(BaseModel):
    model_config = _STRICT
    value: Union[str, float]
    probability: Optional[float] = Field(default=None, ge=0, le=1)
    distribution: Optional[dict[str, float]] = None


class RawJevResponse(BaseModel):
    model_config = _STRICT
    model: str = Field(min_length=1)
    answers: dict[str, RawAnswer]


def build_request(state: DecisionState, workflow: JevWorkflow, state_hash: str,
                  features: Optional[FeatureSnapshot] = None) -> dict:
    """Our internal request. The state cannot hold credentials (contract forbids extras). `state.features` is only
    an id, so the feature VALUES travel beside it: Jev must see the numbers it is deciding on."""
    if features is not None and features.snapshot_id != state.features:
        raise ValueError("features do not belong to this state (snapshot id mismatch)")
    return {
        "workflow": workflow.tag, "model": workflow.model, "state_hash": state_hash,
        "state": state.to_dict(),
        "features": features.to_dict() if features is not None else None,
        "questions": {n: q.model_dump() for n, q in workflow.questions.items()},
    }


def _score(raw: RawJevResponse, name: str) -> float:
    v = raw.answers[name].value
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise JevProtocolError("schema_violation", f"{name}: score must be a number")
    if not 0.0 <= float(v) <= 1.0:
        raise JevProtocolError("schema_violation", f"{name}: score outside [0,1]")
    return float(v)


def _choice(raw: RawJevResponse, wf: JevWorkflow, name: str) -> str:
    v = raw.answers[name].value
    if not isinstance(v, str):
        raise JevProtocolError("schema_violation", f"{name}: choice must be a string")
    if v not in wf.questions[name].options:
        raise JevProtocolError("unknown_enum", f"{name}: {v!r} not in {wf.questions[name].options}")
    return v


def normalise(reply: object, wf: JevWorkflow, state: DecisionState, state_hash: str,
              latency_ms: float) -> Decision:
    """Strict mapping of a raw reply to a Contract D Decision. Raises JevProtocolError."""
    if not isinstance(reply, dict):
        raise JevProtocolError("malformed_json", f"reply is {type(reply).__name__}, not an object")
    try:
        raw = RawJevResponse.model_validate(reply)
    except ValidationError as exc:
        raise JevProtocolError("schema_violation", "raw reply: " + _locs(exc)) from None
    missing = sorted(set(wf.questions) - set(raw.answers))
    extra = sorted(set(raw.answers) - set(wf.questions))
    if missing or extra:
        raise JevProtocolError("schema_violation", f"answers missing={missing} unexpected={extra}")

    # choices first (unknown enum is its own category), then scores
    action = _choice(raw, wf, "action")
    regime = _choice(raw, wf, "market_regime")
    risk = _choice(raw, wf, "risk_level")
    scores = {n: _score(raw, n) for n, q in wf.questions.items() if q.type == "score"}

    dist = raw.answers["action"].distribution
    if dist is None or set(dist) != set(ACTIONS):
        raise JevProtocolError("schema_violation", "action.distribution must cover exactly the six actions")
    if any(not (0.0 <= p <= 1.0) for p in dist.values()):
        raise JevProtocolError("schema_violation", "action.distribution has a probability outside [0,1]")
    if abs(sum(dist.values()) - 1.0) > DISTRIBUTION_TOLERANCE:
        raise JevProtocolError("schema_violation", f"action.distribution sums to {sum(dist.values()):.3f}")

    def qa(name: str) -> dict:
        a = raw.answers[name]
        if wf.questions[name].type == "score":
            return {"answer": f"{scores[name]:.4f}", "probability": scores[name]}
        p = a.probability if a.probability is not None else (dist[a.value] if name == "action" else None)
        if p is None:
            raise JevProtocolError("schema_violation", f"{name}: choice answer lacks a probability")
        return {"answer": a.value, "probability": p}

    try:
        return Decision(
            contract="decision", version="1.0",
            decision_id=derive_id("dec", state_hash, wf.tag, "jev"),
            state_id=state.state_id, state_hash=state_hash, source="jev",
            model=raw.model, workflow_version=wf.tag, action=action,
            # the probability Jev assigns to its chosen action, lowered (never raised) by Jev's own
            # `confidence` when it reports one: the more conservative of the two drives policy
            confidence=(dist[action] if raw.answers["action"].probability is None
                        else min(dist[action], raw.answers["action"].probability)),
            probabilities=dist,
            signals={k: scores[k] for k in ("trend_quality", "entry_quality", "expected_edge", "timing_quality")},
            risk_level=risk,
            questions={k: qa(k) for k in ("market_regime", "trend_quality", "entry_quality",
                                          "breakout_probability", "reversal_probability", "risk_level")},
            latency_ms=latency_ms,
        )
    except ValidationError as exc:
        raise JevProtocolError("schema_violation", "decision: " + _locs(exc)) from None


def _locs(exc: ValidationError) -> str:
    return ", ".join(".".join(str(p) for p in e["loc"]) or "<root>" for e in exc.errors()[:6])


# -------------------------------------------------- the adapter

class JevAdapter:
    def __init__(self, client: JevClient, workflow: JevWorkflow, fallback: FallbackSource, *,
                 timeout_s: float = 3.0, clock: Callable[[], float] = time.monotonic, gateway=None):
        if not timeout_s > 0:
            raise ValueError("timeout_s must be > 0")
        self.client, self.workflow, self.fallback = client, workflow, fallback
        self.timeout_s, self._clock = timeout_s, clock
        # V2-15: the System One gateway answers Jev's questions when Jev fails; the strategy vote is the last resort
        self.gateway = gateway
        self.stats: Counter = Counter()

    def _call(self, request: dict) -> object:
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jev")
        try:
            fut = pool.submit(self.client.evaluate, request, self.timeout_s)
            return fut.result(timeout=self.timeout_s)
        except FutureTimeout:
            raise JevProtocolError("timeout", f"no reply within {self.timeout_s}s") from None
        finally:
            pool.shutdown(wait=False)  # a hung call finishes on its own thread; we never wait for it

    def evaluate(self, state: DecisionState, features: Optional[FeatureSnapshot] = None) -> Decision:
        self.stats["calls"] += 1
        state_hash = state.state_hash()
        t0 = self._clock()
        category, detail = None, ""
        try:
            reply = self._call(build_request(state, self.workflow, state_hash, features))
            latency_ms = (self._clock() - t0) * 1000.0
            if latency_ms > self.timeout_s * 1000.0:  # client ignored its own timeout
                raise JevProtocolError("timeout", f"reply took {latency_ms:.0f}ms")
            decision = normalise(reply, self.workflow, state, state_hash, latency_ms)
            self.stats["jev"] += 1
            self.stats[f"model:{decision.model}"] += 1     # `jev-latest` can silently move to a new build: count it
            log.info("decision source=jev id=%s action=%s conf=%.3f latency_ms=%.0f",
                     decision.decision_id, decision.action, decision.confidence, latency_ms)
            return decision
        except JevProtocolError as exc:
            category, detail = exc.category, exc.detail
        except JevError as exc:
            category, detail = exc.category, type(exc).__name__
        except Exception as exc:  # never crash the hot path
            category, detail = "client_error", type(exc).__name__
        latency_ms = (self._clock() - t0) * 1000.0
        return self._fallback_decision(state, state_hash, category, detail, latency_ms, features)

    def _fallback_decision(self, state: DecisionState, state_hash: str, category: str,
                           detail: str, latency_ms: float, features: Optional[FeatureSnapshot] = None) -> Decision:
        self.stats["fallback"] += 1
        self.stats[f"fallback:{category}"] += 1
        if self.gateway is not None and features is not None:
            try:
                d = self.gateway.decide(state, features, workflow_tag=self.workflow.tag,
                                        reason=f"{category}: {detail}; gateway", latency_ms=latency_ms)
                self.stats["fallback_gateway"] += 1
                log.warning("decision source=fallback(gateway) id=%s action=%s conf=%.3f reason=%s",
                            d.decision_id, d.action, d.confidence, category)
                return d
            except Exception as exc:          # the gateway failed: the strategy vote still answers
                self.stats["gateway_failed"] += 1
                detail = f"{detail}; gateway_failed:{type(exc).__name__}"
        try:
            verdict = aggregate(self.fallback.stances(state), state.position.side)
            action, conf, strategy, note = verdict.action, verdict.confidence, verdict.strategy, verdict.reason
        except Exception as exc:  # the fallback itself failed: safest possible answer
            self.stats["fallback_failed"] += 1
            action, conf, strategy = "NO_TRADE", 0.0, "trend_follower"
            note = f"fallback_failed:{type(exc).__name__}"
        conf = min(conf, FALLBACK_MAX_CONFIDENCE)
        if not math.isfinite(conf):
            conf = 0.0
        reason = f"{category}: {detail}; {note}"[:300]
        decision = Decision(
            contract="decision", version="1.0",
            decision_id=derive_id("dec", state_hash, self.workflow.tag, "fallback"),
            state_id=state.state_id, state_hash=state_hash, source="fallback",
            fallback_reason=reason, fallback_strategy=strategy,
            action=action, confidence=conf, latency_ms=latency_ms,
        )
        log.warning("decision source=fallback id=%s action=%s conf=%.3f reason=%s",
                    decision.decision_id, action, conf, reason)
        return decision
