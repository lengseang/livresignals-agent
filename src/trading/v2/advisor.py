"""In-position advice, layer L2 (docs/loop/designs/V2-15): while a trade is open, ask what to do with it.

Jev answers the POSITION workflow (`crypto_position_v1`: HOLD / TIGHTEN_STOP / REDUCE / EXIT + three yes/no reads),
given the features AND the position (entry, stop, target, last, unrealized R, best excursion in R, minutes held).
If Jev fails in any way, the System One gateway answers the same questions (`Gateway.position_answers`), labelled.

Authority, and why it differs from entries: every action here only REDUCES risk, so it is gated by its own
thresholds (PositionPolicy), not the entry ladder that needs 0.90. The same thresholds apply to Jev and the gateway
(this closes E5: "fallback can never EXIT" trapped positions when Jev was down). Bounds that hold whoever answers:
  * TIGHTEN moves the stop to last -/+ k x ATR, only if that is tighter and stays on the right side of the price
    (the paper book's amend_stop and Contract K refuse anything else);
  * EXIT / REDUCE become reduce-only proposals that still go through the risk engine;
  * nothing here can open, add to, or widen a position.
The L0 bracket and the L1 ratchet keep working regardless of what this layer says or whether it runs.
"""
from __future__ import annotations

import logging
import math
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from trading.v2.adapter import JevError, JevProtocolError
from trading.v2.contracts import DecisionState, FeatureSnapshot
from trading.v2.gateway import Gateway, PositionView
from trading.v2.ids import derive_id
from trading.v2.workflow import POSITION_ACTIONS, PositionWorkflow

log = logging.getLogger("trading.v2.advisor")
SCORES = ("momentum_fading", "reversal_risk", "target_reachable")
DISTRIBUTION_TOLERANCE = 0.05


@dataclass(frozen=True)
class PositionPolicy:
    """UNVALIDATED defaults (owner-tunable): the confidence an answer needs before its risk-reducing action runs."""
    exit_at: float = 0.70
    reduce_at: float = 0.70
    tighten_at: float = 0.60
    tighten_atr_k: float = 1.5
    version: str = "position-policy-v1"

    def __post_init__(self):
        for v in (self.exit_at, self.reduce_at, self.tighten_at):
            if not 0.5 <= v <= 1.0:
                raise ValueError("position thresholds must be in [0.5, 1]: below a coin flip is not a decision")
        if not self.tighten_atr_k > 0:
            raise ValueError("tighten_atr_k must be > 0")


@dataclass(frozen=True)
class PositionAdvice:
    advice_id: str
    source: str                      # jev | gateway
    model: str
    workflow: str
    action: str                      # one of POSITION_ACTIONS
    confidence: float
    probabilities: dict
    scores: dict                     # momentum_fading, reversal_risk, target_reachable
    position: dict                   # the PositionView features the answer was given
    latency_ms: float
    fallback_reason: Optional[str] = None

    def __post_init__(self):
        if self.source not in ("jev", "gateway") or self.action not in POSITION_ACTIONS:
            raise ValueError("invalid advice")
        if not 0.0 <= self.confidence <= 1.0 or not math.isfinite(self.confidence):
            raise ValueError("confidence outside [0,1]")
        if (self.source == "gateway") != (self.fallback_reason is not None):
            raise ValueError("a gateway answer must say why Jev did not answer, and only it")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PositionPlan:
    kind: str                        # hold | tighten | exit | reduce
    reason: str
    new_stop: Optional[float] = None


def normalise_position_reply(raw: object, wf: PositionWorkflow) -> tuple[str, str, float, dict, dict]:
    """Strict: the translated reply (typesafe_client.translate_response shape) -> (model, action, conf, dist, scores)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("answers"), dict) or not isinstance(raw.get("model"), str):
        raise JevProtocolError("malformed_json", "reply lacks model/answers")
    ans = raw["answers"]
    if set(ans) != set(wf.questions):
        raise JevProtocolError("schema_violation", f"answers {sorted(ans)} != {sorted(wf.questions)}")
    a = ans["position_action"]
    action, dist = a.get("value"), a.get("distribution")
    if action not in POSITION_ACTIONS:
        raise JevProtocolError("unknown_enum", f"position_action {action!r}")
    if not isinstance(dist, dict) or set(dist) != set(POSITION_ACTIONS):
        raise JevProtocolError("schema_violation", "position_action distribution must cover the four actions")
    for p in dist.values():
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0.0 <= p <= 1.0:
            raise JevProtocolError("schema_violation", "distribution value outside [0,1]")
    if abs(sum(dist.values()) - 1.0) > DISTRIBUTION_TOLERANCE:
        raise JevProtocolError("schema_violation", "distribution does not sum to 1")
    scores = {}
    for k in SCORES:
        v = ans[k].get("value") if isinstance(ans[k], dict) else None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 <= v <= 1.0:
            raise JevProtocolError("schema_violation", f"{k}: expected a number in [0,1]")
        scores[k] = float(v)
    p = a.get("probability")
    conf = float(dist[action]) if p is None else min(float(dist[action]), float(p))
    return raw["model"], action, conf, {k: float(v) for k, v in dist.items()}, scores


class PositionAdvisor:
    def __init__(self, client, workflow: PositionWorkflow, gateway: Gateway, *, timeout_s: float = 8.0,
                 clock: Callable[[], float] = time.monotonic):
        self.client, self.workflow, self.gateway = client, workflow, gateway
        self.timeout_s, self._clock = timeout_s, clock
        self.stats: Counter = Counter()

    def _call(self, request: dict) -> object:
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jev-pos")
        try:
            return pool.submit(self.client.evaluate, request, self.timeout_s).result(timeout=self.timeout_s)
        except FutureTimeout:
            raise JevProtocolError("timeout", f"no reply within {self.timeout_s}s") from None
        finally:
            pool.shutdown(wait=False)

    def advise(self, state: DecisionState, features: FeatureSnapshot, view: PositionView) -> PositionAdvice:
        if state.position.side == "none":
            raise ValueError("advice is only asked while a position is open")
        if features.snapshot_id != state.features:
            raise ValueError("features do not belong to this state")
        self.stats["calls"] += 1
        h = state.state_hash()
        pos = view.as_features()
        t0 = self._clock()
        try:
            request = {"workflow": self.workflow.tag, "model": self.workflow.model, "state_hash": h,
                       "state": state.to_dict(), "features": features.to_dict(), "position_detail": pos}
            reply = self._call(request)
            ms = (self._clock() - t0) * 1000.0
            if ms > self.timeout_s * 1000.0:
                raise JevProtocolError("timeout", f"reply took {ms:.0f}ms")
            model, action, conf, dist, scores = normalise_position_reply(reply, self.workflow)
            self.stats["jev"] += 1
            return PositionAdvice(derive_id("adv", h, self.workflow.tag, "jev"), "jev", model, self.workflow.tag,
                                  action, conf, dist, scores, pos, ms)
        except JevProtocolError as exc:
            why = f"{exc.category}: {exc.detail}"
        except JevError as exc:
            why = f"{exc.category}: {type(exc).__name__}"
        except Exception as exc:          # never crash the hot path
            why = f"client_error: {type(exc).__name__}"
        ms = (self._clock() - t0) * 1000.0
        self.stats["gateway"] += 1
        a = self.gateway.position_answers(features.to_dict(), state.market.spread_bps, view)
        dist = a["position_action_distribution"]
        action = a["position_action"]
        return PositionAdvice(derive_id("adv", h, self.workflow.tag, "gateway"), "gateway", self.gateway.model.version,
                              self.workflow.tag, action, dist[action], dict(dist),
                              {k: a[k] for k in SCORES}, pos, ms, fallback_reason=why[:300])


def plan(advice: PositionAdvice, view: PositionView, atr_frac: float, policy: PositionPolicy) -> PositionPlan:
    """Advice -> what is actually done. Pure. Below its threshold, an action is a HOLD (and says why)."""
    a, c = advice.action, advice.confidence
    if a == "EXIT":
        return PositionPlan("exit", f"exit {c:.2f} >= {policy.exit_at:.2f}") if c >= policy.exit_at else \
            PositionPlan("hold", f"exit {c:.2f} below {policy.exit_at:.2f}")
    if a == "REDUCE":
        return PositionPlan("reduce", f"reduce {c:.2f} >= {policy.reduce_at:.2f}") if c >= policy.reduce_at else \
            PositionPlan("hold", f"reduce {c:.2f} below {policy.reduce_at:.2f}")
    if a == "TIGHTEN_STOP":
        if c < policy.tighten_at:
            return PositionPlan("hold", f"tighten {c:.2f} below {policy.tighten_at:.2f}")
        if view.stop is None or not (math.isfinite(atr_frac) and atr_frac > 0):
            return PositionPlan("hold", "tighten: no stop or no ATR")
        s = 1.0 if view.side == "LONG" else -1.0
        new = view.last * (1.0 - s * policy.tighten_atr_k * atr_frac)
        if s * (new - view.stop) <= 0:
            return PositionPlan("hold", "tighten: the stop is already tighter than last -/+ k ATR")
        return PositionPlan("tighten", f"tighten {c:.2f} >= {policy.tighten_at:.2f}: stop -> last -/+ "
                                       f"{policy.tighten_atr_k:g} ATR", new_stop=new)
    return PositionPlan("hold", "advice: hold")
