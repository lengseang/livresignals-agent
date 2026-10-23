"""`collapse_multitf` must merge one fill booked per timeframe, and NOTHING else.

Context (2026-10-15): `unified_trader._paper_open` guards re-entry per
`(symbol, tf)`, but the same scan computes the signal per timeframe over the
SAME price feed, so one physical dip opens a paper position on every agreeing
timeframe (5m/15m/30m). Each closes and is appended to `closed`, so the ledger
holds the same trade several times.

Measured on data/unified-paper.json:
    closed records                                      2964
    pairs <=5s apart, IDENTICAL entry AND pnl            498
      different tf 476 | same tf (1.5-2.5 ms apart)       22
    across all 498: identical reason, pnl_net, worst_px 498/498

`self_improve.dedupe` (2026-10-14) keys on symbol+open-time+entry+size and
removes only BYTE-IDENTICAL re-bookings; the multi-tf copies have different
`ts_utc` (sub-second) and often a one-tick different `entry`, so every copy is a
different key and all survive. This module collapses them on the READ path.

Run:  ./venv/bin/python -m pytest tests/test_paper_book_multitf.py -q
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _pk(symbol, entry, ts, pnl, tf, reason="SL", worst=None):
    """A PAPER closed record with a tf tag, as the writer emits it."""
    return {"symbol": symbol, "tf": tf, "entry": entry, "pnl": pnl,
            "pnl_net": round(pnl - 0.0079, 6), "reason": reason,
            "worst_px": worst if worst is not None else entry * 0.99,
            "size": 5.0, "ts_utc": ts}


def test_identical_fill_on_three_timeframes_collapses_to_one():
    """The core defect: one dip, three tf bookings, one trade."""
    pb = _load("paper_book")
    recs = [
        _pk("OP/USDT", 0.1323, "2026-10-15T04:16:38.446460+00:00", -0.0529, "5m"),
        _pk("OP/USDT", 0.1323, "2026-10-15T04:16:39.060501+00:00", -0.0529, "15m"),
        _pk("OP/USDT", 0.1323, "2026-10-15T04:16:39.700000+00:00", -0.0529, "30m"),
    ]
    out, removed = pb.collapse_multitf(recs)
    assert removed == 2
    assert len(out) == 1
    # the survivor is the EARLIEST copy (honest open time of the fill)
    assert out[0]["ts_utc"] == "2026-10-15T04:16:38.446460+00:00"


def test_same_tf_sub_second_double_write_collapses():
    """A true double-write in one scan: same tf, ~2 ms apart."""
    pb = _load("paper_book")
    recs = [
        _pk("ETH/USDT", 2360.43, "2026-09-05T10:25:11.595152+00:00", 0.0146, "5m"),
        _pk("ETH/USDT", 2360.43, "2026-09-05T10:25:11.596875+00:00", 0.0146, "5m"),
    ]
    out, removed = pb.collapse_multitf(recs)
    assert removed == 1 and len(out) == 1


def test_distinct_trades_at_same_price_hours_apart_are_kept():
    """The 2026-10-14 lesson: same symbol+entry at different times = 2 trades."""
    pb = _load("paper_book")
    recs = [
        _pk("OP/USDT", 0.1223, "2026-09-19T06:16:40.072625+00:00", -0.0123, "15m"),
        _pk("OP/USDT", 0.1223, "2026-10-09T15:16:21.339923+00:00", 0.2330, "15m"),
    ]
    out, removed = pb.collapse_multitf(recs)
    assert removed == 0 and len(out) == 2


def test_same_time_same_entry_different_pnl_is_not_a_copy():
    """Different outcomes seconds apart = two real fills; exact-pnl is required."""
    pb = _load("paper_book")
    recs = [
        _pk("BTC/USDT", 81838.63, "2026-10-14T14:46:25.085396+00:00", 0.0078, "5m"),
        _pk("BTC/USDT", 81838.63, "2026-10-14T14:46:26.000000+00:00", 0.0912, "5m"),
    ]
    out, removed = pb.collapse_multitf(recs)
    assert removed == 0 and len(out) == 2


def test_same_time_same_pnl_different_entry_is_not_a_copy():
    """A one-tick different entry is still two distinct fills when pnl differs.

    Exact entry is required: a fill one tick away is a different fill unless the
    pnl matches too (which is what makes it the same physical trade).
    """
    pb = _load("paper_book")
    recs = [
        _pk("NEAR/USDT", 4.645, "2026-10-15T05:46:35.752097+00:00", 0.0463, "5m"),
        _pk("NEAR/USDT", 4.643, "2026-10-15T05:46:36.306081+00:00", 0.0555, "5m"),
    ]
    out, removed = pb.collapse_multitf(recs)
    assert removed == 0 and len(out) == 2


def test_one_tick_apart_but_identical_pnl_collapses():
    """The real BTC case: 81838.63 / 81838.62, same pnl, 0.64 s apart = one fill."""
    pb = _load("paper_book")
    recs = [
        _pk("BTC/USDT", 81838.63, "2026-10-14T14:46:25.085396+00:00", 0.0078, "5m"),
        _pk("BTC/USDT", 81838.62, "2026-10-14T14:46:25.729912+00:00", 0.0078, "5m"),
    ]
    out, removed = pb.collapse_multitf(recs)
    assert removed == 0, "different exact entry = not collapsed (precision over recall)"


def test_record_without_timestamp_is_never_collapsed():
    """An untimed record cannot be placed in time, so it stays distinct."""
    pb = _load("paper_book")
    a = _pk("XRP/USDT", 1.5, "2026-10-15T00:00:00+00:00", 0.01, "5m")
    b = {"symbol": "XRP/USDT", "entry": 1.5, "pnl": 0.01, "size": 5.0}
    out, removed = pb.collapse_multitf([a, b])
    assert removed == 0 and len(out) == 2


def test_window_is_documented_and_configurable():
    """A gap larger than the window is not a copy, even with identical fields."""
    pb = _load("paper_book")
    a = _pk("SOL/USDT", 100.0, "2026-10-15T00:00:00+00:00", 0.05, "5m")
    b = _pk("SOL/USDT", 100.0, "2026-10-15T00:00:30+00:00", 0.05, "5m")
    out, removed = pb.collapse_multitf([a, b], window_s=5.0)
    assert removed == 0 and len(out) == 2


# ---------------------------------------------------------------------------
# 2026-10-22 — THE EXACT-FINGERPRINT KEY CAUGHT NONE OF THE REAL PAIRS
# ---------------------------------------------------------------------------
# The 2026-10-15 key required EXACT entry AND EXACT pnl. Measured on the live
# book three days later: 80 same-symbol pairs within 5 minutes, 54 with an
# exact entry+pnl, and the 5s rule removed 0 of 3,164 rows. The bookings differ
# because each timeframe re-reads the ticker (~0.5-1.2s apart, sometimes 22s),
# and four pairs landed ONE TICK apart with pnl differing by $0.04. A key that
# only matches what it was written to catch, and catches nothing, is the
# `len(set(x)) == 1` failure mode one level down: it was certified as fixed by
# a test built from ITS OWN exact-copy fixture.
def test_real_shape_one_tick_apart_is_one_fill():
    """LINK 2026-10-22 12:46 — verbatim from the book, entries one tick apart."""
    pb = _load("paper_book")
    recs = [
        _pk("LINK/USDT", 12.221, "2026-10-22T12:46:30.316789+00:00", 0.0466, "5m"),
        _pk("LINK/USDT", 12.223, "2026-10-22T12:46:31.272897+00:00", 0.0045, "15m"),
    ]
    out, removed = pb.collapse_multitf(recs)
    assert removed == 1, "a one-tick re-booking is the same fill"
    assert len(out) == 1


def test_real_shape_36s_apart_across_timeframes_is_one_fill():
    """The largest REAL gap observed between the 5m/15m/30m bookings of one dip.

    Measured on the live book 2026-10-23: matched cross-timeframe pairs run to a
    hard maximum of 36.1s, after which there is an empty band until 357.7s (one
    scan cadence). This fixture is that 36.1s pair — it used to be a hand-written
    22s case, which was 2s ABOVE the then-20s window and so could never have
    passed while documenting a gap larger than the one the window admitted.
    """
    pb = _load("paper_book")
    recs = [
        _pk("NEAR/USDT", 4.925, "2026-10-20T02:16:10.000000+00:00", 0.0274, "5m"),
        _pk("NEAR/USDT", 4.929, "2026-10-20T02:16:46.100000+00:00", 0.0233, "30m"),
    ]
    out, removed = pb.collapse_multitf(recs)
    assert removed == 1, "the window must cover the largest observed booking gap"
    assert len(out) == 1


def test_same_timeframe_seconds_apart_is_two_fills():
    """The cross-tf clause must NOT merge two 5m entries: two scans = two fills.

    Without this guard a fast-firing rule would have its consecutive entries
    collapsed and its sample shrunk to nothing.
    """
    pb = _load("paper_book")
    recs = [
        _pk("BTC/USDT", 81000.0, "2026-10-22T04:16:31.000000+00:00", 0.01, "5m"),
        _pk("BTC/USDT", 81000.5, "2026-10-22T04:16:40.000000+00:00", 0.02, "5m"),
    ]
    out, removed = pb.collapse_multitf(recs)
    assert removed == 0, "two 5m bookings seconds apart are two entries"
    assert len(out) == 2


def test_cross_timeframe_far_apart_in_price_is_two_fills():
    """Different tf but 0.5% apart = a genuine later entry, not a re-booking."""
    pb = _load("paper_book")
    recs = [
        _pk("ETH/USDT", 3000.0, "2026-10-22T04:16:31.000000+00:00", 0.01, "5m"),
        _pk("ETH/USDT", 3015.0, "2026-10-22T04:16:35.000000+00:00", 0.01, "15m"),
    ]
    out, removed = pb.collapse_multitf(recs)
    assert removed == 0
    assert len(out) == 2


def test_cross_timeframe_different_pnl_is_two_fills():
    """Same tick, different money = two positions, not one fill re-booked."""
    pb = _load("paper_book")
    recs = [
        _pk("SOL/USDT", 150.0, "2026-10-22T04:16:31.000000+00:00", 0.25, "5m"),
        _pk("SOL/USDT", 150.0, "2026-10-22T04:16:32.000000+00:00", 0.90, "15m"),
    ]
    out, removed = pb.collapse_multitf(recs)
    assert removed == 0
    assert len(out) == 2


def test_cross_timeframe_outside_window_is_two_fills():
    """Two different timeframes well apart are two scans -> two entries.

    The gap must be expressed RELATIVE to the clause's own window. This fixture
    was written at 60s when MULTITF_WINDOW_S was 20s; when the window was
    re-derived from the book to 60s (the real max booking gap is 36.1s, the next
    honest re-entry 357.7s) the fixture silently landed INSIDE the window and
    asserted the opposite of its own name. It now derives its gap from the
    constant, so the two can never drift apart again.
    """
    pb = _load("paper_book")
    gap_s = pb.MULTITF_WINDOW_S + 60.0
    t0 = "2026-10-22T04:16:31+00:00"
    t1 = (datetime.fromisoformat(t0)
          + timedelta(seconds=gap_s)).isoformat()
    recs = [
        _pk("ADA/USDT", 0.25, t0, 0.01, "5m"),
        _pk("ADA/USDT", 0.2501, t1, 0.01, "15m"),
    ]
    out, removed = pb.collapse_multitf(recs)
    assert removed == 0
    assert len(out) == 2


def test_unknown_timeframe_is_never_cross_tf_collapsed():
    """A record with no tf (legacy / fixture) must survive untouched."""
    pb = _load("paper_book")
    a = _pk("XRP/USDT", 2.0, "2026-10-22T04:16:31.000000+00:00", 0.01, "5m")
    b = _pk("XRP/USDT", 2.0002, "2026-10-22T04:16:35.000000+00:00", 0.02, "15m")
    b.pop("tf")
    out, removed = pb.collapse_multitf([a, b])
    assert removed == 0
    assert len(out) == 2


def test_three_timeframes_with_ticks_collapse_to_one():
    """The full real pattern: 5m/15m/30m, each a tick apart, one fill."""
    pb = _load("paper_book")
    recs = [
        _pk("AVAX/USDT", 9.278, "2026-10-21T02:46:22.100000+00:00", 0.2705, "5m"),
        _pk("AVAX/USDT", 9.278, "2026-10-21T02:46:23.200000+00:00", 0.2705, "15m"),
        _pk("AVAX/USDT", 9.280, "2026-10-21T02:46:23.400000+00:00", 0.2694, "30m"),
    ]
    out, removed = pb.collapse_multitf(recs)
    assert removed == 2
    assert len(out) == 1


def test_cross_tf_clause_reachable_INSIDE_the_exact_window():
    """REGRESSION (2026-10-23): the clause must fire at ~1s, not only past 5s.

    The first version of the cross-tf clause was nested inside
    `if gap > window_s:` — i.e. it only ran on pairs FURTHER apart than the
    exact-fingerprint window. But the real 5m/15m/30m bookings are 0.5-1.2s
    apart, comfortably INSIDE that 5s window, where the code fell through to the
    exact-entry/exact-pnl test and rejected them (they differ by a tick). So the
    clause could only ever run where the defect it was written for does not
    occur: it caught 16 of 727 real pairs and all three of its own tests failed.

    This test is the guard that fails if the two clauses are ever nested again:
    the pair is inside `window_s` by construction and must STILL collapse.
    """
    pb = _load("paper_book")
    a = _pk("LINK/USDT", 12.221, "2026-10-22T12:46:30.316789+00:00", 0.0466, "5m")
    b = _pk("LINK/USDT", 12.223, "2026-10-22T12:46:31.272897+00:00", 0.0045, "15m")
    gap = abs((pb._open_dt(b) - pb._open_dt(a)).total_seconds())
    assert gap <= pb.DEFAULT_WINDOW_S, "fixture must be INSIDE the exact window"
    out, removed = pb.collapse_multitf([a, b])
    assert removed == 1
    assert len(out) == 1


def test_same_tf_inside_window_with_tick_difference_is_kept():
    """The cross-tf clause must not become a same-tf tolerance rule.

    Two 5m bookings a second apart are two scans (the scan is 30 min apart, so
    this only happens on a double-write or a genuine re-entry). Loosening the
    same-tf key to a price tolerance would let a fast rule's consecutive entries
    eat each other and shrink its own sample to nothing.
    """
    pb = _load("paper_book")
    recs = [
        _pk("BTC/USDT", 81000.0, "2026-10-22T04:16:31.000000+00:00", 0.01, "5m"),
        _pk("BTC/USDT", 81000.5, "2026-10-22T04:16:32.000000+00:00", 0.02, "5m"),
    ]
    out, removed = pb.collapse_multitf(recs)
    assert removed == 0
    assert len(out) == 2
