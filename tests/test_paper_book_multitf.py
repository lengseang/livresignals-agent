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
