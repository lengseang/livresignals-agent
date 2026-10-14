"""ReplayEngine.run() must terminate when the replay window reaches the end of
the data — the hang that blocked the hunt5 gate test suite.

THE DEFECT (found 2026-10-14)
-----------------------------
`run()` loops:

    while True:
        self._fill_pending()
        now = self._aligned_now()
        if now > self.end_ms:      # <-- STRICT greater-than
            break
        ...
        if not self.advance_all(): # <-- returned True unconditionally
            break

`advance_all()` returned True even when every feed was exhausted and no cursor
could move, and the clock stops at the last bar's close_time. So whenever the
requested `end_ms` is >= the last bar's close_time (i.e. the window reaches or
overshoots the end of the data), `now <= end_ms` stays true forever, nothing
advances, and `run()` spins until the process is killed.

Why it had never shown up: hunt5 sweeps a window ending 2026-10-01, and the DB
data ends after that, so `now` overtakes `end_ms` and the loop exits normally.
The FIRST caller whose window ends at the data end was the hunt5 split-half
fixture (tests/loop/test_hunt5_gate.py), whose END is exactly one bar past its
last close — it hung the suite instead of failing it.

The fix: a feed step that cannot move the clock is exhaustion, and exhaustion
terminates the replay. See advance_all()'s return contract.

Everything here is a synthetic in-memory fixture — REPLAY, not real data.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from replay_engine import Costs, Decision, ReplayEngine  # noqa: E402

STEP = 4 * 60 * 60 * 1000            # one 4h bar, in ms
SYM = "BTC/USDT"


def _ms(s: str) -> int:
    return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


START = "2022-01-01"
NBARS = 50


def _db(nbars: int = NBARS) -> tuple[sqlite3.Connection, int, int]:
    """One contiguous 4h series. Returns (con, first_close_ms, last_close_ms)."""
    con = sqlite3.connect(":memory:")
    con.execute("create table candles (symbol text, timeframe text, open_time_utc "
                "integer, open real, high real, low real, close real, volume real)")
    t0 = _ms(START)
    for i in range(nbars):
        t = t0 + i * STEP
        con.execute("insert into candles values (?,?,?,?,?,?,?,?)",
                    (SYM, "4h", t, 100.0, 100.0, 100.0, 100.0, 1.0))
    return con, t0 + STEP, t0 + nbars * STEP


def _engine(con, start_ms, end_ms) -> ReplayEngine:
    return ReplayEngine(con, [SYM], "4h", start_ms, end_ms,
                        costs=Costs(fee_bps=5.0, slippage_bps=5.0),
                        notional=5.0, max_open=8, bars_cache={})


def _enter_every_bar(feeds, openpos):
    if SYM not in openpos:
        return [Decision(SYM, "enter_short", stop_frac=0.5, target_frac=0.5, hold_bars=1, tag="t")]
    return []


@pytest.mark.parametrize("end_offset_bars", [0, 1, 2, 10])
def test_run_terminates_when_the_window_reaches_the_end_of_data(end_offset_bars):
    """The exact shape that hung the suite: end_ms at/just past the last close."""
    con, first_close, last_close = _db()
    # end_ms at the last close, and one/two/ten bars beyond it.
    end_ms = last_close + (end_offset_bars - 1) * STEP
    eng = _engine(con, _ms(START), end_ms)
    # If the loop does not terminate this raises (pytest-timeout) or hangs the
    # suite; a returned list is the pass condition.
    trades = eng.run(_enter_every_bar)
    assert isinstance(trades, list)


def test_run_terminates_when_no_end_ms_overshoots_the_data():
    """A window entirely INSIDE the data still terminates (regression guard)."""
    con, _, _ = _db()
    eng = _engine(con, _ms(START), _ms(START) + 30 * STEP)
    assert isinstance(eng.run(_enter_every_bar), list)


def test_advance_all_reports_exhaustion_and_cannot_move_the_clock():
    """The signal run() relies on: once every feed is at its last bar, a step
    makes no progress and must say so (return False), not keep saying True."""
    con, _, _ = _db()
    eng = _engine(con, _ms(START), _ms(START) + 10_000 * STEP)
    # drain to the end
    for _ in range(NBARS + 5):
        if not eng.advance_all():
            break
    assert eng.feeds[SYM].exhausted is True
    before = eng.feeds[SYM].cursor_ms
    assert eng.advance_all() is False          # nothing left to move
    assert eng.feeds[SYM].cursor_ms == before  # and it truly did not move


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
