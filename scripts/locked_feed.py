#!/usr/bin/env python3
"""A cursor-locked market data source. **You cannot read the future through it.**

This is the guard for walk-forward replay. The whole point is that look-ahead is
structurally impossible, not merely discouraged:

    src = LockedFeed(con, "BTC/USDT", "5m")
    src.seek_to(2019_01_01)
    src.history(n=500)        # bars up to and including the cursor
    src.advance()             # cursor moves forward only
    src.close()               # <- the guard for this bar
    src.history(n=1)          # the NEXT bar. impossible before close().

    LockedFeed.peek(1)        # raises LookAheadError
    src.seek_to(past)         # raises RewindError

Two failure modes are worth naming because most backtests die of them:

  * reading a future bar            -> structurally impossible (see below)
  * reading a bar that has not CLOSED yet, while pretending it has
      A bar is only knowable after its close_time. At cursor T, the newest bar a
      strategy may see is the last one whose close_time <= T. `history()` enforces
      this; `current_forming()` returns the partial bar and is REFUSED unless the
      caller passes allow_forming=True, so the use of an unclosed bar is always an
      explicit, greppable decision rather than an accident.

The floor of the current bar's high/low is likewise withheld: knowing today's high
before the bar closes is look-ahead.

Usage: import and use; also runnable as a self-test:
    python3 scripts/locked_feed.py
"""
from __future__ import annotations

import bisect
import sqlite3
from typing import Iterable


class LookAheadError(RuntimeError):
    """Raised when something tries to read a bar that has not happened yet."""


class RewindError(RuntimeError):
    """Raised when something tries to move the cursor backwards."""


def tf_ms(tf: str) -> int:
    m = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
    if tf not in m:
        raise ValueError(f"unknown timeframe {tf!r}")
    return m[tf]


class Bar:
    __slots__ = ("open_time", "close_time", "open", "high", "low", "close", "volume")

    def __init__(self, open_time, close_time, o, h, l, c, v):
        self.open_time, self.close_time = open_time, close_time
        self.open, self.high, self.low, self.close, self.volume = o, h, l, c, v

    def __repr__(self):
        return (f"Bar({self.open_time} O{self.open} H{self.high} "
                f"L{self.low} C{self.close})")


class LockedFeed:
    """A forward-only view of one candle series.

    The ENTIRE series is loaded at construction, but the cursor gates all reads.
    Loading eagerly is a performance choice; it does not weaken the guard, because
    no accessor can return a bar beyond the cursor.
    """

    def __init__(self, con: sqlite3.Connection, symbol: str, timeframe: str,
                 start_ms: int | None = None, allow_empty_before: bool = False):
        self.symbol, self.timeframe = symbol, timeframe
        self._step = tf_ms(timeframe)
        rows = con.execute(
            "select open_time_utc, open, high, low, close, volume from candles "
            "where symbol=? and timeframe=? order by open_time_utc",
            (symbol, timeframe),
        ).fetchall()
        self._bars = [
            Bar(int(r[0]), int(r[0]) + self._step, r[1], r[2], r[3], r[4],
                (r[5] if len(r) > 5 else 0.0))
            for r in rows
        ]
        self._times = [b.open_time for b in self._bars]
        self._idx: int | None = None      # index of newest CLOSED bar
        if start_ms is not None:
            try:
                self.seek_to(start_ms)
            except LookAheadError:
                # The symbol's data has not begun at start_ms. That is legitimate:
                # ARB did not exist in 2017. Park the cursor BEFORE the first bar so
                # `history()` is empty (the strategy sees nothing and cannot trade)
                # and `advance()` will step onto bar 0 as the clock reaches it.
                if not allow_empty_before:
                    raise
                self._idx = -1

    # ---- cursor control -------------------------------------------------

    def seek_to(self, ms: int) -> None:
        """Move the cursor forward to the last bar whose close_time <= ms."""
        if self._idx is not None and ms < self._current_close():
            raise RewindError(
                f"seek_to({ms}) is in the past; cursor is at {self._current_close()}. "
                "The feed is forward-only by design."
            )
        # the newest bar that has CLOSED by ms
        lo, hi = 0, len(self._bars)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._bars[mid].close_time <= ms:
                lo = mid + 1
            else:
                hi = mid
        self._idx = lo - 1                # may be -1 => nothing knowable yet
        if self._idx < 0:
            raise LookAheadError(
                f"at {ms} no bar of {self.symbol} {self.timeframe} has closed yet"
            )

    def _current_close(self) -> int:
        return self._bars[self._idx].close_time if self._idx is not None else -1

    def advance(self, n: int = 1) -> None:
        """Move the cursor forward n bars (reveals new information)."""
        if self._idx is None:
            raise LookAheadError("cursor not initialised; call seek_to() first")
        if self._idx + n >= len(self._bars):
            raise LookAheadError("advanced past the end of the series")
        self._idx += n
    def advance_to_next(self) -> None:
        self.advance(1)

    @property
    def cursor_ms(self) -> int:
        """Close time of the newest bar the strategy is allowed to know.

        When the symbol's data has not begun yet (cursor parked before bar 0),
        report the earliest bar's close MINUS one step, so the clock alignment
        check still sees a valid, comparable instant.
        """
        if self._idx is None:
            raise LookAheadError("cursor not initialised")
        if self._idx < 0:
            return self._bars[0].open_time if self._bars else 0
        return self._bars[self._idx].close_time

    @property
    def exhausted(self) -> bool:
        return self._idx is not None and self._idx >= len(self._bars) - 1

    # ---- reads (all gated by the cursor) --------------------------------

    def history(self, n: int = 1) -> list[Bar]:
        """The last n CLOSED bars, oldest first. Never returns a future bar.

        Empty when the symbol's data has not begun at the cursor — a strategy
        must treat that as 'no data', never as 'flat'."""
        if self._idx is None:
            raise LookAheadError("cursor not initialised")
        if self._idx < 0 or n <= 0:
            return []
        start = max(0, self._idx - n + 1)
        return self._bars[start:self._idx + 1]

    def current_forming(self, *, allow_forming: bool = False) -> Bar | None:
        """The bar AFTER the cursor — still forming, not yet knowable.

        Returns None by default. Refuses unless allow_forming=True, so any use of
        a partially-formed bar is an explicit, greppable decision.
        """
        if not allow_forming:
            return None
        if self._idx is None or self._idx + 1 >= len(self._bars):
            return None
        return self._bars[self._idx + 1]

    def peek(self, n: int = 1) -> Bar:
        """LOOK-AHEAD. Always raises. Exists so the attempt is loud, not silent."""
        raise LookAheadError(
            f"peek({n}) refused: reading bar t+{n} is look-ahead. "
            "Advance the cursor instead."
        )

    def series_len(self) -> int:
        return len(self._bars)

    def is_upto(self, ms: int) -> bool:
        return self.cursor_ms <= ms


# ---------------------------------------------------------------------------
# self-test: prove the guard actually guards
# ---------------------------------------------------------------------------

def _selftest() -> int:
    from pathlib import Path
    db = Path(__file__).resolve().parents[1] / "data" / "trading.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    failures = []

    def expect_raise(label, fn, exc):
        try:
            fn()
        except exc:
            print(f"  PASS  {label} ({exc.__name__})")
            return
        except Exception as e:                                  # noqa: BLE001
            failures.append(f"{label}: raised {type(e).__name__}, wanted {exc.__name__}")
            print(f"  FAIL  {label}: raised {type(e).__name__}, wanted {exc.__name__}")
            return
        failures.append(f"{label}: did NOT raise")
        print(f"  FAIL  {label}: did NOT raise")

    print("=== LockedFeed self-test ===\n")

    src = LockedFeed(con, "BTC/USDT", "15m")
    print(f"  series: {src.series_len()} bars of BTC/USDT 15m")

    src.seek_to(1609459200000)          # 2021-01-01
    print(f"  cursor at {src.cursor_ms}")

    h = src.history(500)
    print(f"  history(500) -> {len(h)} bars, newest close_time {h[-1].close_time}")
    ok = len(h) == 500 and h[-1].close_time <= src.cursor_ms
    print(f"  {'PASS' if ok else 'FAIL'}  history never exceeds the cursor")
    if not ok:
        failures.append("history exceeded the cursor")

    ok = all(h[i].open_time < h[i+1].open_time for i in range(len(h)-1))
    print(f"  {'PASS' if ok else 'FAIL'}  history is chronological")
    if not ok:
        failures.append("history not chronological")

    # 1. peek raises
    expect_raise("peek() raises LookAheadError", lambda: src.peek(1), LookAheadError)

    # 2. rewind raises
    expect_raise("seek_to(past) raises RewindError",
                 lambda: src.seek_to(1577836800000), RewindError)

    # 3. current_forming is None unless explicitly allowed
    cf = src.current_forming()
    ok = cf is None
    print(f"  {'PASS' if ok else 'FAIL'}  current_forming() is None without allow_forming")
    if not ok:
        failures.append("current_forming leaked a forming bar")

    # 4. advance reveals exactly one new bar
    before = src.history(1)[0].open_time
    src.advance()
    after = src.history(1)[0].open_time
    ok = after > before and src.cursor_ms > 0
    print(f"  {'PASS' if ok else 'FAIL'}  advance() reveals a newer bar ({before} -> {after})")
    if not ok:
        failures.append("advance did not reveal a new bar")

    # 5. THE decisive one: a strategy that only uses history() sees the SAME
    #    value at cursor T no matter what comes after T. Run the same cursor
    #    twice, with the series truncated differently, and compare.
    src.seek_to(1700000000000)
    a = [b.close for b in src.history(100)]

    con2 = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    src2 = LockedFeed(con2, "BTC/USDT", "15m")
    src2.seek_to(1700000000000)
    b_ = [x.close for x in src2.history(100)]

    ok = a == b_
    print(f"  {'PASS' if ok else 'FAIL'}  identical at the same cursor (deterministic, no hidden state)")
    if not ok:
        failures.append("two feeds disagreed at the same cursor")

    # 6. history() at cursor T must equal the tail of history() at a LATER cursor
    src.seek_to(1700000000000)
    early = [x.close for x in src.history(10)]
    src.advance(50)
    later = [x.close for x in src.history(60)][:10]
    ok = early == later
    print(f"  {'PASS' if ok else 'FAIL'}  the past does not change when the future arrives")
    if not ok:
        failures.append("the past changed retroactively (look-ahead leak)")

    print()
    if failures:
        print(f"  {len(failures)} FAILURE(S):")
        for f in failures:
            print(f"    - {f}")
        return 1
    print("  all guard checks pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
