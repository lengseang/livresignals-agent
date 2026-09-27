"""The scanner loop: ticks -> BarBuilder -> instant alignment -> replay's LoopCore.step -> signal records.

The strategy loop is not re-implemented here: every closed instant goes through
trading.replay.engine.LoopCore.step, the same body replay() runs, with the same Context, BarStore,
resamplers and paper account. Only the source of bars differs.

Release rule (what makes live instants equal replay instants):
- Each symbol has a `progress`: the exchange time of its last accepted tick. Because ticks per symbol
  arrive in order, every event that symbol can still produce is later than its progress. A symbol whose
  current minute has started but has not traded yet caps its progress just below that minute's open,
  because its OPEN event is still to come.
- An instant t is released to LoopCore when every symbol's progress is >= t, so all closes and opens at t
  (every symbol) enter the loop together, exactly as replay groups them. Offline this is exact.
- Live, a silent symbol must not hold everyone hostage: with `grace_ms`, instants up to
  (max progress over symbols - grace) are released anyway, and bars of symbols whose minute ended that
  long ago are closed. A CLOSE that arrives after its instant was released is dropped and counted
  (`late_events`): it is never applied to the past. A late OPEN (an illiquid symbol's first trade came
  after the grace release) still fills pending paper orders at its open price, as replay does, at an
  opens-only instant equal to the time it became known; no strategy runs at that instant.

Paper only: the account is a PaperAccount (simulated fills at the next 1m open, as in replay), needed so
ctx.position() answers exactly as it does in replay. There is no order path.
"""
from __future__ import annotations

import heapq
import itertools
import time
from collections.abc import Iterable, Mapping

from trading.replay.broker import CostModel, PaperAccount
from trading.replay.engine import LoopCore
from trading.scanner.bars import BarBuilder
from trading.scanner.registry import DEFAULT, Registry

MODES = ("LIVE_PAPER", "REPLAY")
_KIND = {"close": 0, "open": 1}          # at one instant closes (and decisions) precede opens (fills)
_SIDE = {"L": "LONG", "S": "SHORT"}


class Scanner:
    def __init__(self, symbols: Iterable[str], strategies: list,
                 accounts: Mapping[str, PaperAccount] | None = None, *, mode: str = "LIVE_PAPER",
                 run_id: str | None = None, sink=None, source: str = "kline_1s", grace_ms: int | None = 3000,
                 registry: Registry | None = DEFAULT, keep_bars: bool = False, keep_1m: int = 1440):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
        self.symbols = sorted(set(symbols))
        if not self.symbols:
            raise ValueError("no symbols")
        self.mode, self.sink, self.grace_ms, self.registry = mode, sink, grace_ms, registry
        if accounts is None:
            # cost numbers only size paper positions; the real fee rate and spreads are UNKNOWN (T2.3)
            accounts = {"paper": PaperAccount(1000.0, len(self.symbols), CostModel(0.001, 2.0, 2.0),
                                              name="paper", mode=mode)}
        self.core = LoopCore(self.symbols, strategies, accounts, mode=mode, run_id=run_id, keep_1m=keep_1m)
        self.builder = BarBuilder(source)
        self._q: list = []
        self._n = itertools.count()
        self._progress: dict[str, int | None] = {s: None for s in self.symbols}
        self._max_progress: int | None = None
        self.released: int | None = None               # last instant stepped
        self.closed_bars = {s: [] for s in self.symbols} if keep_bars else None
        self.stats = {"ticks": 0, "foreign_ticks": 0, "instants": 0, "late_events": 0, "bars_closed": 0,
                      "late_opens_applied": 0, "signals": 0}

    # --- input -----------------------------------------------------------------------------------
    def on_tick(self, tick) -> list[dict]:
        """Feed one parsed Tick. Returns the signal records emitted as a result (possibly none)."""
        sym = tick.symbol
        if sym not in self._progress:
            self.stats["foreign_ticks"] += 1
            return []
        self.stats["ticks"] += 1
        self._push(self.builder.feed(tick))
        t = self.builder.last_time
        if t is not None:
            p = self._progress[sym]
            if p is None or t > p:
                self._progress[sym] = t
            if self._max_progress is None or t > self._max_progress:
                self._max_progress = t
        if self.grace_ms is not None and self._max_progress is not None:
            cutoff = self._max_progress - self.grace_ms
            for s in self.symbols:
                self._push(self.builder.close_through(s, cutoff))
        return self._release(self._watermark(), tick.ts_recv_ns)

    def finish(self) -> list[dict]:
        """End of an OFFLINE input: release every queued event. Bars still open are never closed here
        (a recording cut mid-minute must not become a partial bar)."""
        return self._release(2**62, None)

    def reconnected(self) -> int:
        """The live stream reconnected: bars in progress have a hole, so they are dropped (a gap)."""
        return self.builder.discard_open()

    # --- alignment ---------------------------------------------------------------------------------
    def _push(self, events: list[tuple]) -> None:
        for t, kind, sym, obj in events:
            heapq.heappush(self._q, (t, _KIND[kind], sym, next(self._n), obj))

    def _watermark(self) -> int | None:
        wm = None
        for s, p in self._progress.items():
            if p is None:
                wm = None
                break
            cap = self.builder.pending_open_ms(s)
            v = p if cap is None else min(p, cap - 1)
            wm = v if wm is None or v < wm else wm
        if self.grace_ms is not None and self._max_progress is not None:
            g = self._max_progress - self.grace_ms
            wm = g if wm is None or g > wm else wm
        return wm

    def _release(self, wm: int | None, recv_ns: int | None) -> list[dict]:
        out: list[dict] = []
        q = self._q
        while q and wm is not None and q[0][0] <= wm:
            t = q[0][0]
            group = []
            while q and q[0][0] == t:
                group.append(heapq.heappop(q))
            if self.released is not None and t <= self.released:
                self._late(group)
                continue
            closed = [g[4] for g in group if g[1] == 0]
            opens = [g[4] for g in group if g[1] == 1]
            if self.closed_bars is not None:
                for b in closed:
                    self.closed_bars[b.symbol].append(b)
            self.stats["bars_closed"] += len(closed)
            fired = self.core.step(t, closed, opens)
            self.released = t
            self.stats["instants"] += 1
            for strat, sig in fired:
                out.append(self._emit(strat, sig, recv_ns))
            self.core.res.fills = {name: a.fills for name, a in self.core.accounts.items()}
        return out

    def _late(self, group: list) -> None:
        """Events whose instant was already released (live, after a grace release).

        A late CLOSE is dropped (the bar is lost: a gap, never applied to the past). A late OPEN only
        carries the first trade price of a bar that started before now; it still fills pending paper
        orders, as replay would, at an opens-only instant = the time it became known (no strategy runs).
        """
        opens = [g[4] for g in group if g[1] == 1]
        self.stats["late_events"] += len(group) - len(opens)
        t_known = self.builder.last_time
        if opens and t_known is not None and t_known > self.released:
            self.core.step(t_known, [], sorted(opens, key=lambda o: o.symbol))
            self.released = t_known
            self.stats["late_opens_applied"] += len(opens)
        else:
            self.stats["late_events"] += len(opens)

    # --- output ------------------------------------------------------------------------------------
    def _meta(self, strat) -> dict:
        try:
            info = self.registry.get(strat.name) if self.registry is not None else None
        except KeyError:
            info = None
        if info is None:
            return {"family": "UNKNOWN", "strategy_version": "UNKNOWN", "gate_status_at_fire": "PENDING",
                    "evidence": "UNKNOWN"}
        return {"family": info.family, "strategy_version": info.version,
                "gate_status_at_fire": info.gate_status, "evidence": info.evidence}

    def _emit(self, strat, sig, recv_ns: int | None) -> dict:
        now_ns = time.time_ns()
        latency = None
        if self.mode == "LIVE_PAPER" and recv_ns:
            latency = round((now_ns - recv_ns) / 1e6, 3)       # same local clock: receive -> emit
        m = self._meta(strat)
        rec = {
            "v": 1, "type": "signal", "ts_emit_ms": now_ns // 1_000_000, "ts_event_ms": sig.ts_ms,
            "mode": self.mode, "simulated": True,
            "data": {
                "signal_id": f"{sig.strategy}:{sig.symbol}:{sig.ts_ms}", "strategy_id": sig.strategy,
                "strategy_version": m["strategy_version"], "family": m["family"],
                "params": getattr(strat, "params", {}), "symbol": sig.symbol, "side": _SIDE[sig.side],
                "action": sig.action, "interval": sig.interval, "ref_price": sig.ref_price,
                # P4a: a target-weight signal's weight (share of equity to hold; 0 = exit); absent otherwise
                **({"weight": sig.weight} if getattr(sig, "weight", None) is not None else {}),
                "reason": sig.reason,
                # T6.1 fields this strategy cannot know yet: UNKNOWN (null), never a made-up number
                "class": None, "tp_pct": None, "sl_pct": None, "expected_move_pct": None,
                "cost_rt_pct": None, "p_star": None,
                "gate_status_at_fire": m["gate_status_at_fire"], "evidence": m["evidence"],
                "latency_ms": {"tick_to_signal": latency},
            },
        }
        self.stats["signals"] += 1
        if self.sink is not None:
            rec = self.sink.write(rec)
        return rec
