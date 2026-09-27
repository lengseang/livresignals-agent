"""ShadowFleet: one tick stream fanned out to one isolated paper book per candidate (P2 design §1-2, §5).

LoopCore sends every strategy's signals to every account it holds, so candidates never share a core: each gets its
own Scanner (LoopCore, BarStore, PaperAccount) and its own ShadowChain. After each tick the fleet copies the book's
new broker events into the chain as `shadow_fill`, and at every UTC day boundary writes a `shadow_mark` with the
book's equity. A candidate whose on_bar raises is stopped and recorded (`error`); the others continue.
Paper only: nothing here imports an exchange client (tests/loop/conftest.py blocks it).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from trading.replay.broker import CostModel, PaperAccount
from trading.scanner.registry import DEFAULT, Registry
from trading.scanner.scanner import Scanner
from trading.shadow.chain import ShadowChain, utc_day

LANES = ("candidate", "rejected", "benchmark")
DAY = 86_400_000


@dataclass
class Book:
    lane: str
    name: str
    scanner: Scanner
    account: PaperAccount
    chain: ShadowChain
    drained: int = 0
    last_mark_day: str | None = None
    stopped: str | None = None
    signals: int = 0
    stats: dict = field(default_factory=dict)


class ShadowFleet:
    def __init__(self, symbols, candidates: list[dict], root: Path | str, forward_start_ms: int, *,
                 registry: Registry = DEFAULT, equity: float = 1000.0, costs: CostModel | None = None,
                 grace_ms: int | None = 3000):
        """candidates: [{"lane": "candidate"|"rejected"|"benchmark", "strategy": name, "params": {}, "slots": n}]"""
        self.symbols = sorted(set(symbols))
        self.books: list[Book] = []
        self.root, self._scan_minute = Path(root), None      # P4c: live scan written once per minute
        costs = costs or CostModel(0.001, 2.0, 2.0)   # P2a: flat model; P2b switches to the lab's paper fill model
        for c in candidates:
            if c["lane"] not in LANES:
                raise ValueError(f"lane must be one of {LANES}")
            strat = registry.build(c["strategy"], **c.get("params", {}))
            acct = PaperAccount(equity, c.get("slots") or len(self.symbols), costs, name=f"{c['lane']}:{c['strategy']}",
                                mode="LIVE_PAPER")
            chain = ShadowChain(root, c["lane"], c["strategy"], forward_start_ms)
            sc = Scanner(self.symbols, [strat], {"paper": acct}, mode="LIVE_PAPER", sink=chain, grace_ms=grace_ms,
                         registry=registry)
            self.books.append(Book(c["lane"], c["strategy"], sc, acct, chain))

    # --- stream --------------------------------------------------------------------------------------------
    def on_tick(self, tick) -> None:
        self._maybe_scan(getattr(tick, "ts_exchange_ms", None))
        for b in self.books:
            if b.stopped:
                continue
            try:
                b.signals += len(b.scanner.on_tick(tick))
            except Exception as e:  # noqa: BLE001 - one candidate's bug must not stop the others
                self._stop(b, e, tick.ts_exchange_ms if hasattr(tick, "ts_exchange_ms") else None)
                continue
            self._drain(b)

    def finish(self) -> None:
        for b in self.books:
            if not b.stopped:
                try:
                    b.signals += len(b.scanner.finish())
                except Exception as e:  # noqa: BLE001
                    self._stop(b, e, None)
                    continue
                self._drain(b)

    def gap(self, from_ms: int, to_ms: int, reason: str) -> None:
        """The stream dropped or the PC slept: record it everywhere; bars in progress are discarded (no back-fill)."""
        for b in self.books:
            b.chain.write({"v": 1, "type": "gap", "ts_event_ms": to_ms, "mode": "LIVE_PAPER", "simulated": True,
                           "data": {"from_ms": from_ms, "to_ms": to_ms, "reason": reason}})
            if not b.stopped:
                b.scanner.reconnected()

    def _maybe_scan(self, t_ms: int | None) -> None:
        if t_ms is None:
            return
        minute = t_ms // 60_000
        if self._scan_minute is not None and minute == self._scan_minute:
            return
        first = self._scan_minute is None
        self._scan_minute = minute
        if not first:                                          # never during the first (catch-up) instant
            from trading.shadow.scan import write_scans
            write_scans(self, self.root, t_ms)

    # --- internals -----------------------------------------------------------------------------------------
    def _stop(self, b: Book, e: Exception, t_ms: int | None) -> None:
        b.stopped = f"{type(e).__name__}: {str(e)[:200]}"
        b.chain.write({"v": 1, "type": "error", "ts_event_ms": t_ms or b.chain.forward_start_ms, "mode": "LIVE_PAPER",
                       "simulated": True, "data": {"error": b.stopped, "action": "candidate stopped"}})

    def _drain(self, b: Book) -> None:
        new = b.account.events[b.drained:]
        b.drained = len(b.account.events)
        for e in new:
            if e.get("type") in ("signal", "close", "rebalance"):
                b.chain.write({"v": 1, "type": "shadow_fill", "ts_event_ms": e["ts_ms"], "mode": "LIVE_PAPER",
                               "simulated": True, "data": {"fill_type": e["type"], **e.get("payload", {})}})
        t = b.scanner.core.clock.now_ms
        if not t or t < b.chain.forward_start_ms:          # the engine clock starts at 0 before the first instant
            return
        day = utc_day(t)
        if b.last_mark_day is None:
            b.last_mark_day = day
        elif day != b.last_mark_day:
            b.chain.write({"v": 1, "type": "shadow_mark", "ts_event_ms": t, "mode": "LIVE_PAPER", "simulated": True,
                           "data": {"day": b.last_mark_day, "equity": b.account.equity(),
                                    "cash": b.account.cash, "positions": sorted(b.account.positions)}})
            b.last_mark_day = day
