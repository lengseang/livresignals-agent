#!/usr/bin/env python3
"""A parameter must mean the same thing to every book that reads it.

THE DEFECT (found 2026-11-02, autonomous improvement pass)
----------------------------------------------------------
`data/exit-params.json` carries `disaster_stop_pct` = 0.025, measured by
`exit_calibrate.py` on `data/live-trades.json`. Its own SCOPE GUARD says the
file belongs to that one book:

    "scope": "live-trades.json (dip-buy spot executor) ONLY"

`unified_trader._paper_open()` read it anyway and used it as the INITIAL stop
for the paper book — `sl = close * (1 - stop)` and `tp = close * (1 + 2*stop)`.
The two books want opposite things from a stop: the executor wants a
DISASTER stop (as wide as possible, only there to bound a tail), the paper
opener wants an INITIAL stop (as tight as the edge allows, RR is fixed at
2:1). One scalar served both. Measured on the live book while writing this:

    stop_pct read by the paper opener            0.025
    paper stop width the comment/study set up   0.020
    live stop width as of 2026-09-19             -0.010

So the paper book silently re-floored its stop from 2% to 2.5% when the
calibrator moved, with RR pinned at 2.0 — every paper entry from then on was
booked with a TP 5% away instead of 4%. Nothing announced it: the opener
prints the prices, not the source of the width.

WHY THIS IS A LESSON AND NOT A TYPO
-----------------------------------
It is the same class as #9/#13/#15 in SYSTEM_MAP: a value is read at a BASIS
other than the one it was measured on. SYSTEM_MAP rule 18 states it for a
GATE ("a gate must name the population it protects"); this is the same rule
applied to a KNOB. The write-side guard ("scope") was documented and then
ignored by a reader — a scope field nobody enforces is a comment.

WHAT THIS FILE PINS
-------------------
1. The paper book owns its stop width, and a calibrator move cannot touch it.
2. RR is derived from the same width (one threshold, not two).
3. The live book still gets `disaster_stop_pct` — the fix must not swing the
   pendulum and hand the executor the paper's tighter stop.
4. The fix is reachable where the defect lived: the OPENER, not a helper
   nobody calls (SYSTEM_MAP #16).
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

UNIFIED = ROOT / "scripts" / "unified_trader.py"


def _write_exit_params(path: Path, stop: float) -> Path:
    path.write_text(json.dumps({"scope": "live-trades.json ONLY",
                                "disaster_stop_pct": stop, "hold_hours": 2.0}))
    return path


def test_paper_stop_does_not_track_the_live_calibrator(tmp_path, monkeypatch):
    """A 2.5% disaster stop on the live book must not widen the paper stop.

    RED before the fix: `_paper_open()` took `_exit_params()["stop_pct"]`, so
    the paper entry was booked with sl = -2.5% and tp = +5.0%.
    """
    import live_executor as le
    import unified_trader as ut

    p = _write_exit_params(tmp_path / "exit-params.json", 0.025)
    monkeypatch.setattr(le, "_EXIT_PARAMS_PATH", p, raising=True)
    # The live calibrator DOES see the moved number...
    assert le._exit_params()["stop_pct"] == pytest.approx(0.025)

    # ...and the paper opener does not, because it no longer asks it.
    got = ut._paper_exit_params()
    assert got["stop_pct"] == pytest.approx(ut.PAPER_INITIAL_STOP_PCT)

    _write_exit_params(p, 0.08)          # a wildly different disaster stop
    assert ut._paper_exit_params()["stop_pct"] == pytest.approx(
        ut.PAPER_INITIAL_STOP_PCT), "paper width moved with the live file"


def test_paper_rr_is_derived_from_the_paper_stop():
    """One threshold: tp is `rr` x the stop actually used, never a second constant."""
    import unified_trader as ut

    params = ut._paper_exit_params()
    stop, rr = params["stop_pct"], params["rr"]
    entry = 100.0
    sl = round(entry * (1 - stop), 4)
    tp = round(entry * (1 + stop * rr), 4)
    assert (entry - sl) / entry == pytest.approx(stop)
    assert (tp - entry) / entry == pytest.approx(stop * rr)


def test_opener_does_not_import_the_live_calibrator():
    """The fix must be in the opener, not a helper the opener never calls.

    A source-level assertion because the defect was a CALL, not a value:
    `if pnl < 0`-style logic can be right and still never run. SYSTEM_MAP #16.
    """
    tree = ast.parse(UNIFIED.read_text(encoding="utf-8"))
    opener = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "_paper_open"), None)
    assert opener is not None, "_paper_open moved; update this test"
    called = {n.func.id for n in ast.walk(opener)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_exit_params" not in called, (
        "_paper_open is reading the LIVE calibrator again — the paper stop is "
        "an initial stop, the calibrator's is a disaster stop")
    imported = {a.name for n in ast.walk(opener) if isinstance(n, ast.ImportFrom)
                for a in n.names}
    imported |= {(n.module or "") for n in ast.walk(opener) if isinstance(n, ast.ImportFrom)}
    assert not any("live_executor" in m for m in imported), (
        "_paper_open imports the live executor again")


def test_live_book_still_reads_the_disaster_stop(tmp_path, monkeypatch):
    """The fix must not hand the executor the paper's tighter stop."""
    import live_executor as le

    p = _write_exit_params(tmp_path / "exit-params.json", 0.025)
    monkeypatch.setattr(le, "_EXIT_PARAMS_PATH", p, raising=True)

    assert le._exit_params()["stop_pct"] == pytest.approx(0.025)


def test_paper_stop_stays_inside_the_proven_envelope():
    """The 2% initial stop is the value the 2026-09-16 split-half study measured.

    Not a magic number: the paired replay (+1.896 IS / +1.811 OOS, t=+3.58)
    covers 1.0-2.0% and the hard cap is 3.0%. A change here needs its own
    study, so pin the envelope rather than the exact float.
    """
    import unified_trader as ut

    assert 0.010 <= ut.PAPER_INITIAL_STOP_PCT <= 0.030
    assert ut.PAPER_RR >= 1.0
    assert ut.PAPER_INITIAL_STOP_PCT <= ut.PAPER_STOP_CAP
