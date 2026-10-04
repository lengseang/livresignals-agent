"""The deterministic kill controller (Gate 6): no model, no LLM, no clock, no I/O.

Hermes may REQUEST a halt; this controller decides. It watches facts the caller observes and
LATCHES the kill switch when any of these holds (the note's "emergency" list):
  exchange down (N consecutive bad health observations), feed stale, runaway order rate,
  unexpected position, price-feed corruption, abnormal slippage, daily loss exceeded.
An UNKNOWN required observation is itself a trigger: this is a fail-closed switch.
Once engaged it stays engaged until a human calls `reset(by=...)`. Nothing Hermes can send
disengages it, places an order, or closes a position.

Halt requests (Contract J) can only RESTRICT: they add a pause (all / symbol / strategy) that blocks NEW
entries in the risk engine; exits are never blocked by a pause. Resuming is human-only. A request below
`min_request_confidence` is rejected (and logged). REDUCE_EXPOSURE is recorded as a pending request for
the policy layer; the controller itself never trades.

KillLimits has NO defaults: these thresholds are unmeasured, so the owner / runner supplies them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Protocol

from trading.v2.contracts import HaltRequest
from trading.v2.risk import ExchangeHealth


class Sink(Protocol):
    def write(self, rec: dict) -> dict: ...


@dataclass(frozen=True)
class KillLimits:
    exchange_failures_to_engage: int
    max_feed_age_ms: int
    max_orders_per_minute: int
    max_abs_slippage_bps: float
    max_price_move_frac: float          # one-observation move treated as feed corruption
    daily_loss_cap_usd: Optional[float]
    min_request_confidence: float       # halt requests below this are rejected


@dataclass(frozen=True)
class Observation:
    now_ms: int
    exchange: Optional[ExchangeHealth]
    feed_age_ms: Optional[int]
    orders_last_minute: Optional[int]
    unexpected_positions: Optional[tuple[str, ...]]
    recent_slippage_bps: Optional[float]     # None = no fills in the window (not "unknown")
    price: Optional[float]
    prev_price: Optional[float]
    daily_pnl: Optional[float]


@dataclass(frozen=True)
class KillState:
    engaged: bool
    reasons: tuple[str, ...]
    newly_engaged: bool
    engaged_at_ms: Optional[int]


@dataclass(frozen=True)
class HaltDecision:
    accepted: bool
    detail: str


def _bad_number(x) -> bool:
    return x is None or not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x)


class KillController:
    def __init__(self, limits: KillLimits, sink: Optional[Sink] = None):
        if limits.exchange_failures_to_engage < 1:
            raise ValueError("exchange_failures_to_engage must be >= 1")
        self.limits, self.sink = limits, sink
        self.engaged = False
        self.reasons: list[str] = []
        self.engaged_at_ms: Optional[int] = None
        self.paused: set[tuple[str, str]] = set()
        self.reduce_requests: list[HaltRequest] = []
        self._bad_health = 0
        self.log: list[dict] = []

    # ------------------------------------------------------------------ observation

    def _triggers(self, o: Observation) -> list[str]:
        lim, t = self.limits, []
        if o.exchange is None or not o.exchange.ok:
            self._bad_health += 1
            if self._bad_health >= lim.exchange_failures_to_engage:
                t.append("exchange_down" if o.exchange is not None else "unknown_exchange")
        else:
            self._bad_health = 0
        if _bad_number(o.feed_age_ms):
            t.append("unknown_feed_age")
        elif o.feed_age_ms > lim.max_feed_age_ms:
            t.append("feed_stale")
        if _bad_number(o.orders_last_minute):
            t.append("unknown_order_rate")
        elif o.orders_last_minute > lim.max_orders_per_minute:
            t.append("runaway_order_rate")
        if o.unexpected_positions is None:
            t.append("unknown_positions")
        elif o.unexpected_positions:
            t.append("unexpected_position:" + ",".join(sorted(o.unexpected_positions)))
        if o.recent_slippage_bps is not None:
            if _bad_number(o.recent_slippage_bps) or abs(o.recent_slippage_bps) > lim.max_abs_slippage_bps:
                t.append("abnormal_slippage")
        if _bad_number(o.price) or o.price <= 0:
            t.append("price_invalid")
        elif not _bad_number(o.prev_price) and o.prev_price > 0 and abs(o.price / o.prev_price - 1) > lim.max_price_move_frac:
            t.append("price_jump")
        if _bad_number(o.daily_pnl):
            t.append("unknown_daily_pnl")
        elif lim.daily_loss_cap_usd is not None and o.daily_pnl <= -abs(lim.daily_loss_cap_usd):
            t.append("daily_loss_exceeded")
        return t

    def observe(self, o: Observation) -> KillState:
        triggers = self._triggers(o)
        newly = False
        if triggers and not self.engaged:
            self.engaged, newly, self.engaged_at_ms = True, True, o.now_ms
            self._emit("engaged", o.now_ms, reasons=triggers)
        for r in triggers:                       # latched: later triggers are still recorded
            if r not in self.reasons:
                self.reasons.append(r)
        return KillState(self.engaged, tuple(self.reasons), newly, self.engaged_at_ms)

    def reset(self, by: str, now_ms: int) -> None:
        """Human reset. Does not clear pauses."""
        if not by or not by.strip():
            raise ValueError("reset requires the name of the human who is resetting")
        self._emit("reset", now_ms, by=by, cleared=list(self.reasons))
        self.engaged, self.reasons, self.engaged_at_ms, self._bad_health = False, [], None, 0

    # ------------------------------------------------------------------ halt requests (Contract J)

    def request_halt(self, req: HaltRequest, now_ms: int) -> HaltDecision:
        if req.confidence < self.limits.min_request_confidence:
            d = HaltDecision(False, f"confidence {req.confidence} below minimum {self.limits.min_request_confidence}")
            self._emit("halt_rejected", now_ms, request=req.to_dict(), detail=d.detail)
            return d
        if req.action == "REDUCE_EXPOSURE":
            self.reduce_requests.append(req)
            d = HaltDecision(True, "reduce-exposure request recorded for the policy layer; nothing traded here")
        else:
            scope = {"HALT_ALL": ("all", "*"), "HALT_SYMBOL": ("symbol", req.target or ""),
                     "HALT_STRATEGY": ("strategy", req.target or "")}[req.action]
            self.paused.add(scope)
            d = HaltDecision(True, f"new entries paused for {scope[0]}={scope[1]}")
        self._emit("halt_accepted", now_ms, request=req.to_dict(), detail=d.detail)
        return d

    def resume(self, scope: tuple[str, str], by: str, now_ms: int) -> bool:
        if not by or not by.strip():
            raise ValueError("resume requires the name of the human who is resuming")
        had = scope in self.paused
        self.paused.discard(scope)
        self._emit("resume", now_ms, by=by, scope=list(scope), had_pause=had)
        return had

    @property
    def paused_scopes(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self.paused))

    def _emit(self, event: str, ts_ms: int, **fields) -> None:
        rec = {"mode": "PAPER_V2", "type": "kill_event", "real": False, "event": event, "ts_event_ms": ts_ms,
               **fields}
        self.log.append(rec)
        if self.sink is not None:
            self.sink.write(rec)
