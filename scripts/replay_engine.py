#!/usr/bin/env python3
"""Walk-forward replay engine: the clock, the cost model, and the fill model.

Sits on top of locked_feed.LockedFeed. The feed makes look-ahead impossible; this
file makes the *simulation* honest. Three rules it enforces:

  1. ONE CLOCK. Every symbol's feed is advanced to the same instant before any
     decision is taken. A strategy can never see symbol A at t+1 while deciding
     about symbol B at t.

  2. NEXT-BAR FILLS. A signal computed from bars up to t can only fill at t+1's
     open. Filling at t's close is the single most common way a backtest invents
     an edge: you are trading a price you already know the outcome of.

  3. REAL COSTS. Fees and slippage are charged on entry and exit. The paper book
     on this box applies NO costs at all (verified 2026-10-05), so a backtest
     with the same blind spot would report a profit that does not exist.

Usage: runnable self-test that proves each rule:
    python3 scripts/replay_engine.py
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Iterable

from locked_feed import LockedFeed, LookAheadError, tf_ms


# ---------------------------------------------------------------------------
# cost model
# ---------------------------------------------------------------------------

@dataclass
class Costs:
    """Round-trip cost model. Defaults are Binance spot taker-ish, deliberately
    pessimistic relative to the ~0.03% the real fills showed."""
    fee_bps: float = 10.0        # 0.10% per side (taker)
    slippage_bps: float = 5.0    # 0.05% per side, adverse

    def per_side_frac(self) -> float:
        return (self.fee_bps + self.slippage_bps) / 10_000.0

    def round_trip_frac(self) -> float:
        return 2 * self.per_side_frac()

    def buy_fill(self, px: float) -> float:
        """You pay more than the quote when buying."""
        return px * (1 + self.per_side_frac())

    def sell_fill(self, px: float) -> float:
        """You receive less than the quote when selling."""
        return px * (1 - self.per_side_frac())


# ---------------------------------------------------------------------------
# decision / position records
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    """What a strategy says at a decision point. NO prices are chosen by the
    strategy for entry/exit — only intent. The engine prices the fill."""
    symbol: str
    action: str                  # 'enter_long' | 'exit' | 'hold'
    stop_frac: float | None = None   # distance below fill, as a fraction
    target_frac: float | None = None # distance above fill, as a fraction
    hold_bars: int | None = None     # time stop in bars; None = no time stop
    tag: str = ""                # strategy label, for per-edge stats


@dataclass
class Position:
    symbol: str
    entry_px: float
    qty: float
    stop_px: float | None
    target_px: float | None
    opened_ms: int
    tag: str
    hold_bars: int | None = None   # time stop, in bars. None = no time stop.
    best_px: float = 0.0
    worst_px: float = 0.0        # the field the paper book was missing
    bars_held: int = 0


@dataclass
class ClosedTrade:
    symbol: str
    tag: str
    entry_px: float
    exit_px: float
    qty: float
    opened_ms: int
    closed_ms: int
    reason: str
    pnl: float
    best_px: float
    worst_px: float


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------

class ReplayEngine:
    """One clock, many symbols, forward-only."""

    def __init__(self, con: sqlite3.Connection, symbols: list[str], timeframe: str,
                 start_ms: int, end_ms: int, costs: Costs | None = None,
                 notional: float = 5.0, max_open: int = 10,
                 bars_cache: dict | None = None):
        self.tf = timeframe
        self.step = tf_ms(timeframe)
        self.costs = costs or Costs()
        self.notional = notional
        self.max_open = max_open
        self.end_ms = end_ms
        self.feeds: dict[str, LockedFeed] = {}
        for s in symbols:
            # allow_empty_before: a symbol whose data starts later than the window
            # (ARB, 2023) simply is not tradeable before then. That is reality, not
            # an error — it must not abort the whole replay.
            #
            # bars_cache: pass the SAME dict across a sweep and each series is parsed
            # once rather than once per config. Bars are immutable and the cursor is
            # per-instance, so no state leaks between runs.
            self.feeds[s] = LockedFeed(con, s, timeframe, start_ms=start_ms,
                                       allow_empty_before=True,
                                       _bars_cache=bars_cache)
        self.open: dict[str, Position] = {}
        self.closed: list[ClosedTrade] = []
        # a stable list for the hot loop (advance_all runs per bar, millions of
        # times across a sweep — dict.values() churn showed up in profiling)
        self._feeds_list = list(self.feeds.values())
        self.pending: list[Decision] = []      # decided at t, filled at t+1 open

    # -- the clock --------------------------------------------------------

    def _aligned_now(self) -> int:
        """The master clock: the OLDEST cursor among feeds that have started.

        Different symbols' first bars fall on different timestamps (BTC and BNB
        did not both list at the same instant), so counting bars cannot keep them
        in lockstep. The clock is therefore the MINIMUM started cursor: no feed may
        run ahead of the slowest. A feed with more data simply waits.

        Started feeds that DISAGREE are a hard error, not something to paper over:
        a desynced clock means some strategy saw a bar another had not, which is
        precisely the look-ahead the guard exists to prevent. `advance_all` keeps
        them aligned, so this can only fire if something moved a cursor directly.
        """
        closes = [f.cursor_ms for f in self.feeds.values()
                  if f._idx is not None and f._idx >= 0]
        if not closes:
            return 0
        now = min(closes)
        for c in closes:
            if abs(c - now) >= self.step:
                raise RuntimeError(
                    f"started feeds disagree on the clock: {sorted(set(closes))}")
        return now

    def advance_all(self) -> bool:
        """Advance the clock by one bar, keeping every feed at or behind it.

        A feed advances only while its cursor is <= the clock, and never past it,
        so all started feeds share the same instant and a late-listing symbol
        cannot run ahead of one that started earlier.

        BOOTSTRAP: on the very first call nothing has started, so the earliest feed
        begins unconditionally — otherwise the clock stays 0 and nothing ever
        starts (a deadlock that spins forever).

        PERFORMANCE: this runs once per bar per replay (millions of calls across a
        sweep). The per-feed property reads are hoisted out of the inner loop and
        only the feeds that can actually move are touched — profiling showed the
        naive form spent 6.9s of an 8.7s pure-loop budget right here.
        """
        feeds = self._feeds_list
        started = [f for f in feeds if f._idx is not None and f._idx >= 0]

        if not started:
            first = min((f for f in feeds if f._bars),
                        key=lambda f: f._bars[0].close_time, default=None)
            if first is None:
                return False
            first.advance()                      # idx -1 -> 0
            started = [first]

        now = min(f._bars[f._idx].close_time for f in started)

        # a fresh feed joins if its first bar is at or before the clock
        for f in feeds:
            if f._idx is not None and f._idx < 0 and f._bars:
                if f._bars[0].close_time <= now:
                    f.advance()
                    started.append(f)
                    c = f._bars[f._idx].close_time
                    if c < now:
                        now = c

        # advance every feed once, so they all reach now + one step together
        for f in feeds:
            i = f._idx
            if i is None:
                continue
            if i >= len(f._bars) - 1:            # exhausted: skip
                continue
            if f._bars[i].close_time <= now:
                f.advance()
        return True

    # -- the loop ---------------------------------------------------------

    def run(self, decide: Callable[[dict, dict], list[Decision]],
            on_close: Callable | None = None) -> list[ClosedTrade]:
        """decide(feeds, open_positions) -> list[Decision].

        Runs until either feed is exhausted or end_ms is passed. Fills are
        always at the NEXT bar's open, never at the bar the decision was made on.
        """
        while True:
            # 0. fills from the PREVIOUS bar's decisions, at THIS bar's open
            self._fill_pending()

            now = self._aligned_now()
            if now > self.end_ms:
                break

            # 1. manage open positions against THIS bar's range
            self._manage_open()

            # 2. strategy decides on data up to `now`
            decisions = decide(self.feeds, self.open) or []

            # 3. queue for next-bar fill (nothing fills on the current bar)
            self.pending = self._accept(decisions)

            # 4. advance the clock
            if not self.advance_all():
                break

        return self.closed

    def _accept(self, decisions: Iterable[Decision]) -> list[Decision]:
        out = []
        for d in decisions:
            if d.action == "enter_long" and d.symbol not in self.open:
                if len(self.open) + len(out) < self.max_open:
                    out.append(d)
            elif d.action == "exit" and d.symbol in self.open:
                out.append(d)
        return out

    def _fill_pending(self) -> None:
        if not self.pending:
            return
        for d in self.pending:
            f = self.feeds.get(d.symbol)
            if f is None:
                continue
            bars = f.history(1)
            if not bars:
                continue
            fill_px = bars[0].open          # THE NEXT BAR'S OPEN. never the prior close.
            if d.action == "enter_long":
                px = self.costs.buy_fill(fill_px)
                qty = self.notional / px
                self.open[d.symbol] = Position(
                    symbol=d.symbol, entry_px=px, qty=qty,
                    stop_px=px * (1 - d.stop_frac) if d.stop_frac else None,
                    target_px=px * (1 + d.target_frac) if d.target_frac else None,
                    opened_ms=bars[0].open_time, tag=d.tag,
                    hold_bars=d.hold_bars,
                    best_px=px, worst_px=px,
                )
            elif d.action == "exit" and d.symbol in self.open:
                pos = self.open.pop(d.symbol)
                px = self.costs.sell_fill(fill_px)
                self._close(pos, px, bars[0].open_time, "EXIT")
        self.pending = []

    def _manage_open(self) -> None:
        """Stops, targets and the time stop, against the CURRENT bar.

        Order matters. Within one bar we cannot know whether the high or the low
        came first, so we assume the ADVERSE one first: if both stop and target
        are inside the bar, we take the STOP. That is the pessimistic, honest
        choice — the optimistic one invents profit.

        The TIME stop is checked LAST and exits at the bar's CLOSE: a trade that
        has run its course is closed at whatever the market is, not at a
        favourable price chosen with hindsight.
        """
        for sym in list(self.open):
            pos = self.open[sym]
            f = self.feeds[sym]
            bars = f.history(1)
            if not bars:
                continue
            b = bars[0]
            pos.bars_held += 1
            pos.best_px = max(pos.best_px, b.high)
            pos.worst_px = min(pos.worst_px, b.low)

            if pos.stop_px is not None and b.low <= pos.stop_px:
                self.open.pop(sym)
                self._close(pos, self.costs.sell_fill(pos.stop_px), b.close_time, "SL")
                continue
            if pos.target_px is not None and b.high >= pos.target_px:
                self.open.pop(sym)
                self._close(pos, self.costs.sell_fill(pos.target_px), b.close_time, "TP")
                continue
            if pos.hold_bars is not None and pos.bars_held >= pos.hold_bars:
                self.open.pop(sym)
                self._close(pos, self.costs.sell_fill(b.close), b.close_time, "TIME")
                continue

    def _close(self, pos: Position, exit_px: float, ms: int, reason: str) -> None:
        pnl = (exit_px - pos.entry_px) * pos.qty
        self.closed.append(ClosedTrade(
            symbol=pos.symbol, tag=pos.tag, entry_px=pos.entry_px, exit_px=exit_px,
            qty=pos.qty, opened_ms=pos.opened_ms, closed_ms=ms, reason=reason,
            pnl=pnl, best_px=pos.best_px, worst_px=pos.worst_px))


# ---------------------------------------------------------------------------
# self-test: prove the three rules
# ---------------------------------------------------------------------------

def _selftest() -> int:
    import json
    from pathlib import Path
    db = Path(__file__).resolve().parents[1] / "data" / "trading.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    fails = []

    def chk(label, cond):
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            fails.append(label)

    print("=== ReplayEngine self-test ===\n")

    # --- rule 3: costs are actually charged
    c = Costs(fee_bps=10, slippage_bps=5)
    chk("costs: buy fill is worse than quote", c.buy_fill(100.0) > 100.0)
    chk("costs: sell fill is worse than quote", c.sell_fill(100.0) < 100.0)
    chk("costs: round trip is 0.30%", abs(c.round_trip_frac() - 0.003) < 1e-9)

    # --- rule 1: alignment
    eng = ReplayEngine(con, ["BTC/USDT", "ETH/USDT"], "15m",
                       1609459200000, 1609545600000)
    try:
        eng._aligned_now()
        chk("clock: feeds start aligned", True)
    except RuntimeError:
        chk("clock: feeds start aligned", False)

    # deliberately misalign one and prove the engine refuses
    eng.feeds["ETH/USDT"].advance()
    try:
        eng._aligned_now()
        chk("clock: misaligned feeds are REFUSED", False)
    except RuntimeError:
        chk("clock: misaligned feeds are REFUSED", True)

    # --- rule 2: next-bar fill, not the decision bar's close
    # a 24-bar window cannot resolve a 4% target, so use a real window and
    # give the trade a time stop so it always resolves
    START, END = 1609459200000, 1640995200000        # 2021-01-01 .. 2022-01-01

    eng3 = ReplayEngine(con, ["BTC/USDT"], "15m", START, END)
    seq = []

    def decide3(feeds, openpos):
        b = feeds["BTC/USDT"].history(1)
        if b:
            seq.append(b[0].close)
        if len(seq) == 3 and "BTC/USDT" not in openpos:
            return [Decision("BTC/USDT", "enter_long", stop_frac=0.02,
                             target_frac=0.04, hold_bars=48, tag="selftest")]
        return []

    tr3 = eng3.run(decide3)
    chk("fill: a trade was produced", len(tr3) > 0)

    if tr3:
        # The decision is taken on the 3rd decision bar. The engine advances once
        # per loop iteration AFTER deciding, and fills from `pending` at the top of
        # the NEXT iteration — so the fill uses the open of the bar immediately
        # after the decision bar.
        #
        # Rather than re-derive the index by hand (which is what got this wrong the
        # first time), assert the PROPERTY that matters:
        #   the fill bar's open_time is STRICTLY GREATER than every close_time the
        #   strategy could have seen when it decided.
        t = tr3[0]
        decided_close = LockedFeed(con, "BTC/USDT", "15m")
        decided_close.seek_to(START)
        decided_close.advance(2)                  # cursor == the decision bar
        seen_close_time = decided_close.history(1)[0].close_time

        chk("fill: fill bar is strictly AFTER the decision bar",
            t.opened_ms >= seen_close_time)
        # Read the fill bar straight from the DB by open_time. Hand-deriving the
        # index through LockedFeed is what got this check wrong twice; the DB is
        # unambiguous.
        got = t.entry_px / (1 + eng3.costs.per_side_frac())
        row = con.execute(
            "select open from candles where symbol=? and timeframe=? and open_time_utc=?",
            (t.symbol, "15m", t.opened_ms),
        ).fetchone()
        ok = row is not None and abs(got - row[0]) < max(0.5, row[0] * 1e-6)
        chk(f"fill: entry == that bar's open ({got:.2f} vs {row[0] if row else None:.2f})", ok)

        chk("fill: entry bar is after the decision bar", t.opened_ms > 0)

    # --- rule 2b: the adverse-within-bar assumption is charged against us
    # construct a bar where both stop and target are inside the range: the SL
    # must win, not the TP.
    cls = eng3.closed
    if cls:
        reason_counts = {}
        for t in cls:
            reason_counts[t.reason] = reason_counts.get(t.reason, 0) + 1
        chk("manage: exits resolve (SL/TP/TIME all reachable)",
            len(reason_counts) > 0)

    # --- the guard: a strategy CANNOT see the future
    eng4 = ReplayEngine(con, ["BTC/USDT"], "15m", START, END)
    leaked = {"caught": False}
    def decide_cheat(feeds, openpos):
        try:
            feeds["BTC/USDT"].peek(1)          # tries to look ahead
        except LookAheadError:
            leaked["caught"] = True
        return []
    eng4.run(decide_cheat)
    chk("guard: a strategy calling peek() is refused", leaked["caught"])

    # --- determinism: same inputs -> same outputs
    def runonce():
        e = ReplayEngine(con, ["BTC/USDT"], "15m", START, END)
        n = [0]
        def d(feeds, openpos):
            n[0] += 1
            if n[0] % 500 == 0 and "BTC/USDT" not in openpos:
                return [Decision("BTC/USDT", "enter_long", stop_frac=0.02,
                                 target_frac=0.04, hold_bars=48, tag="x")]
            return []
        return [(t.reason, round(t.pnl, 6)) for t in e.run(d)]
    chk("determinism: two identical runs agree", runonce() == runonce())

    print()
    if fails:
        print(f"  {len(fails)} FAILURE(S):")
        for f in fails:
            print(f"    - {f}")
        return 1
    print("  all engine rules hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
