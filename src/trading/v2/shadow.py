"""Shadow pipeline (V2.0): market event -> features -> state -> Jev adapter -> policy -> risk -> LOG ONLY.

NOTHING EXECUTES. This module has no execution dependency: it imports no exchange client and no order
path (tests/loop's import guard enforces it). Where the chain would have traded, it records an
`execution_preview` (a Contract H object built from the approved risk decision) and nothing more.

Every cycle writes ONE record to an append-only, hash-chained sink (`trading.shadow.chain.ShadowChain`):
the full contracts at every step (event, snapshot id / what was missing, state + hash, decision, policy +
reasons, proposal, risk decision with every check, preview) plus what the CURRENT system would have done, so
a disagreement is data. Records are labelled shadow / not real. Paper and real are never mixed.

It must prove it ran: `stats` counts every cycle and outcome, a heartbeat summary is available, a sink
failure RAISES (a shadow that cannot persist did not run), and a crash inside a cycle is recorded as
`cycle_error` with its stage rather than lost. After a restart, cycles already in the chain are skipped, never logged twice.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional, Protocol

from trading.replay.bars import Bar
from trading.replay.context import BarStore
from trading.v2.adapter import JevAdapter
from trading.v2.contracts import (
    ExecutionCommand, PositionState, RiskState, StrategyRef, UnusableSnapshot,
)
from trading.v2.events import iso_ms, kline_closed_event
from trading.v2.features import BookSource, compute_features
from trading.v2.ids import derive_id
from trading.v2.policy import ExitParams, PolicyConfig, evaluate_policy, propose_trade
from trading.v2.risk import ExchangeHealth, OpenOrder, RiskContext, RiskLimits, evaluate_risk
from trading.v2.state import build_decision_state

log = logging.getLogger("trading.v2.shadow")

INACTIVE = ("HOLD", "NO_TRADE")


@dataclass(frozen=True)
class CycleContext:
    """Everything a cycle needs that is not market data. The caller supplies it (paper book, exchange
    health probe, kill-switch file...). Unknowns are None and the risk engine fails closed on them."""
    position: PositionState
    risk_state: RiskState
    strategy: StrategyRef
    exit_params: Optional[ExitParams]
    day_start_equity: float
    week_start_equity: float
    high_water_mark: float
    open_notional: float = 0.0
    open_orders: tuple[OpenOrder, ...] = ()
    exchange: Optional[ExchangeHealth] = None
    kill_switch_engaged: Optional[bool] = None
    strategy_loss_pct: float = 0.0
    consecutive_losses: int = 0
    daily_loss_budget_used: float = 0.0
    leverage: float = 1.0
    legacy_action: Optional[str] = None     # what the CURRENT system would do now (None = unavailable)


class ContextProvider(Protocol):
    def context(self, symbol: str, as_of_ms: int) -> CycleContext: ...


class Sink(Protocol):
    def write(self, rec: dict) -> dict: ...


def compare_with_legacy(action: str, legacy: Optional[str]) -> dict:
    if legacy is None:
        return {"kind": "no_legacy", "v2": action, "legacy": None}
    norm = lambda a: "INACTIVE" if a in INACTIVE else a  # noqa: E731
    kind = "agree" if norm(action) == norm(legacy) else "disagree"
    return {"kind": kind, "v2": action, "legacy": legacy}


class ShadowPipeline:
    def __init__(self, *, adapter: JevAdapter, provider: ContextProvider, sink: Sink,
                 book: Optional[BookSource], policy: PolicyConfig = PolicyConfig(),
                 limits: RiskLimits, cadence_minutes: int = 15, max_stale_ms: int = 10_000,
                 source: str = "ws", venue: str = "binance", store: Optional[BarStore] = None):
        if cadence_minutes < 1:
            raise ValueError("cadence_minutes must be >= 1")
        self.adapter, self.provider, self.sink, self.book = adapter, provider, sink, book
        self.policy, self.limits = policy, limits
        self.cadence_ms = cadence_minutes * 60_000
        self.max_stale_ms, self.source, self.venue = max_stale_ms, source, venue
        self.store = store or BarStore()
        self.stats: Counter = Counter()
        # After a restart, bars are replayed: skip cycles already in the chain (never log one twice).
        self.resume_after_ms: int = int(getattr(sink, "resumed_from_ms", None) or 0)

    # ------------------------------------------------------------------ feed

    def on_bar(self, bar: Bar, received_ms: int) -> Optional[dict]:
        """Feed one CLOSED bar. Returns the written record if this bar triggered a decision cycle."""
        try:
            self.store.append(bar)
        except ValueError as exc:   # duplicate / out-of-order bar (e.g. a feed reconnect replays one)
            self.stats["bar_rejected"] += 1
            log.warning("bar rejected %s %s end=%s: %s", bar.symbol, bar.interval, bar.end_ms, exc)
            return None
        self.stats["bars"] += 1
        if bar.interval != "1m" or bar.end_ms % self.cadence_ms != 0:
            return None
        return self._cycle(bar, received_ms)

    def run(self, bars: Iterable[Bar], latency_ms: int = 0) -> list[dict]:
        """Replay helper: bars ordered by close time, LONGER intervals first at the same instant so a
        decision at an hour boundary sees the hourly bar that closed with it."""
        recs = []
        order = sorted(bars, key=lambda b: (b.end_ms, -(b.end_ms - b.open_ms)))
        for b in order:
            r = self.on_bar(b, b.end_ms + latency_ms)
            if r is not None:
                recs.append(r)
        return recs

    # ------------------------------------------------------------------ one cycle

    def _cycle(self, bar: Bar, received_ms: int) -> Optional[dict]:
        if bar.end_ms <= self.resume_after_ms:
            self.stats["skipped_already_logged"] += 1
            return None
        event = kline_closed_event(bar, received_ms, source=self.source, venue=self.venue)
        self.stats["cycles"] += 1
        rec: dict = {
            "mode": "shadow", "type": "v2_decision_cycle", "real": False,
            "label": "SHADOW (paper, not real money)", "ts_event_ms": bar.end_ms,
            "ts_iso": iso_ms(bar.end_ms), "symbol": bar.symbol, "event": event.to_dict(),
        }
        stage = ["features"]
        try:
            self._fill(rec, bar, received_ms, stage)
        except Exception as exc:        # a crash inside a cycle is RECORDED, never lost
            self.stats["cycle_error"] += 1
            log.exception("shadow cycle failed at stage %s", stage[0])
            rec.update(outcome="cycle_error", stage=stage[0], error=type(exc).__name__)
        return self._emit(rec)          # outside the try: a sink failure propagates

    def _fill(self, rec: dict, bar: Bar, received_ms: int, stage: list) -> None:
        res = compute_features(bar.symbol, self.store, self.book, received_ms)
        if not res.ok:
            rec.update(outcome="no_snapshot", missing=list(res.missing))
            return
        snap = res.snapshot
        stage[0] = "context"
        ctx = self.provider.context(bar.symbol, bar.end_ms)
        stage[0] = "state"
        try:
            state = build_decision_state(
                snap, price=res.last_close, spread_bps=res.spread_bps, position=ctx.position,
                risk=ctx.risk_state, strategy=ctx.strategy, max_stale_ms=self.max_stale_ms)
        except UnusableSnapshot as exc:
            rec.update(outcome="snapshot_unusable", reason=str(exc), features=snap.to_dict())
            return
        stage[0] = "decision"
        decision = self.adapter.evaluate(state)
        stage[0] = "policy"
        pres = evaluate_policy(decision, self.policy)
        prop = propose_trade(decision, pres, state, ctx.exit_params)
        risk = preview = None
        if prop.proposal is not None:
            stage[0] = "risk"
            rctx = RiskContext(
                state=state, day_start_equity=ctx.day_start_equity, week_start_equity=ctx.week_start_equity,
                high_water_mark=ctx.high_water_mark, open_notional=ctx.open_notional,
                open_orders=ctx.open_orders, exchange=ctx.exchange,
                kill_switch_engaged=ctx.kill_switch_engaged, strategy_loss_pct=ctx.strategy_loss_pct,
                consecutive_losses=ctx.consecutive_losses,
                daily_loss_budget_used=ctx.daily_loss_budget_used, leverage=ctx.leverage)
            risk = evaluate_risk(prop.proposal, rctx, pres.policy.adjusted_confidence * 100.0, self.limits)
            if risk.approved:
                preview = ExecutionCommand.from_approved(
                    proposal=prop.proposal, risk_decision=risk, venue=self.venue,
                    command_id=derive_id("ex", prop.proposal.proposal_id, risk.risk_decision_id))
        cmp = compare_with_legacy(decision.action, ctx.legacy_action)
        self.stats[f"source:{decision.source}"] += 1
        self.stats[f"compare:{cmp['kind']}"] += 1
        rec.update(
            outcome=self._outcome(pres.policy.tier, prop.proposal, prop.reason, risk),
            features={"snapshot_id": snap.snapshot_id, "feature_version": snap.feature_version},
            state=state.to_dict(), state_hash=state.state_hash(), decision=decision.to_dict(),
            policy=pres.policy.to_dict(), policy_reasons=list(pres.reasons),
            proposal=prop.proposal.to_dict() if prop.proposal else None, proposal_reason=prop.reason,
            risk=risk.to_dict() if risk else None,
            execution_preview=preview.to_dict() if preview else None,
            comparison=cmp,
            versions={"feature": snap.feature_version, "workflow": self.adapter.workflow.tag,
                      "policy": self.policy.policy_version, "risk": self.limits.risk_version,
                      "model": decision.model})

    @staticmethod
    def _outcome(tier: str, proposal, proposal_reason: str, risk) -> str:
        if tier == "IGNORE":
            return "ignored"
        if tier == "WATCH":
            return "watch"
        if tier == "STRATEGY_CHECK":
            return "escalate"
        if proposal is None:
            return "no_proposal"
        return "would_execute" if risk is not None and risk.approved else "risk_rejected"

    def _emit(self, rec: dict) -> dict:
        try:
            written = self.sink.write(rec)
        except Exception as exc:  # a shadow that cannot persist did not run: fail loudly
            self.stats["sink_error"] += 1
            log.error("shadow sink failed: %s", type(exc).__name__)
            raise
        self.stats[f"outcome:{rec['outcome']}"] += 1
        self.stats["records"] += 1
        log.info("shadow cycle symbol=%s ts=%s outcome=%s", rec["symbol"], rec["ts_iso"], rec["outcome"])
        return written

    def summary(self) -> dict:
        """Heartbeat: proves the pipeline ran and what it did."""
        s = dict(self.stats)
        s["jev_share"] = (self.stats["source:jev"] / (self.stats["source:jev"] + self.stats["source:fallback"])
                          if (self.stats["source:jev"] + self.stats["source:fallback"]) else None)
        return s
