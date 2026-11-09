"""T0.16 — a window that judges a dead edge must end where the edge died.

The 2026-11-08 fix shipped `_recent_edge_net()` to put a recent net beside a
lifetime net on a STOPPED line. It windowed `[now - 28d, now]`, which is
structurally empty for a stopped edge: `loose` last filled 2026-10-11T23:16Z,
so a window ending at report time opens after its final trade. The clause was
absent from every real report while its unit tests — which injected the map by
hand — were green. These tests run the real computation end to end.
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import improvement_loop as il  # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0)
# 29 days ago: outside the 28d now-window AND past `_edge_is_live`'s 14d
# stale threshold, so the edge is classified DEAD by the real code path. The
# 2026-11-08 fix hard-coded dates around 2026-10-11 and silently rotted into
# "live" once the calendar moved on; these are relative on purpose.
DEAD_LAST = NOW - timedelta(days=29)


def _t(ts, pnl, loose=True, edge=None):
    d = {"symbol": "SUI/USDT", "pnl": pnl, "size": 5.0,
         "ts_utc": ts.isoformat(), "reason": "TP", "entry": 1.0}
    if edge:
        d["edge"] = edge
    else:
        d["loose"] = loose
    return d


def _dead_book():
    """A stop-shaped book: a dead `loose` edge that stopped 29 days ago.

    The 28d window ending at NOW contains ZERO of these rows — the exact live
    condition on 2026-11-09.
    """
    rows = [
        _t(DEAD_LAST - timedelta(days=d), -0.05) for d in (1, 3, 5, 9, 20)
    ]
    rows += [_t(NOW - timedelta(days=d), -0.01, loose=False) for d in (1, 2, 3)]
    return rows


# ---------------------------------------------------------- the live regression

def test_unanchored_window_excludes_a_stopped_edge():
    """The bug, pinned: a now-anchored 28d window cannot see a dead edge."""
    got = il._recent_edge_net(_dead_book(), 28, now=NOW)
    assert "loose" not in got, (
        "a STOPPED edge has no fills in [now-28d, now]; if this ever contains "
        "loose the fixture stopped modelling the live defect"
    )


def test_anchored_window_finds_the_stopped_edge():
    got = il._recent_edge_net(_dead_book(), 28, now=NOW,
                              anchors={"loose": DEAD_LAST.isoformat()})
    assert "loose" in got, (
        "anchoring at the last fill is the whole fix: without this key the "
        "shipped clause silently degrades and the report lies again"
    )
    assert got["loose"]["n"] == 5
    assert got["loose"]["anchored"] is True
    assert got["loose"]["net"] < 0


def test_anchor_bounds_the_window_on_both_sides():
    """Nothing after the anchor may vote — the window ends where the edge died."""
    rows = _dead_book() + [_t(DEAD_LAST + timedelta(days=1), -99.0)]
    got = il._recent_edge_net(rows, 28, anchors={"loose": DEAD_LAST.isoformat()})
    assert got["loose"]["n"] == 5, "a post-anchor fill leaked into the window"
    assert got["loose"]["net"] > -10, "the -99 fill after the anchor was counted"


def test_unanchored_edge_is_byte_identical_to_before():
    """Live edges keep the now-anchored meaning — no behaviour change."""
    rows = [_t(NOW - timedelta(days=d), 0.02, loose=False) for d in (1, 5, 10)]
    got = il._recent_edge_net(rows, 28, now=NOW)
    assert got["validated"]["n"] == 3
    assert got["validated"]["anchored"] is False


def test_undateable_anchor_falls_back_to_now_not_to_silence():
    rows = [_t(NOW - timedelta(days=1), 0.02, loose=False)]
    got = il._recent_edge_net(rows, 28, now=NOW, anchors={"validated": "nonsense"})
    assert got["validated"]["n"] == 1, (
        "an unparseable anchor must degrade to the stated now-window, not drop "
        "the edge (a missing number is honest; a missing CLAUSE is not)"
    )
    assert got["validated"]["anchored"] is False


def test_anchor_older_than_the_window_still_returns_nothing_rather_than_lying():
    rows = [_t(NOW - timedelta(days=200), -0.5)]
    got = il._recent_edge_net(rows, 28, now=NOW,
                              anchors={"loose": (NOW - timedelta(days=300)).isoformat()})
    assert got == {}, "a genuinely empty anchored window must stay empty, not borrow rows"


def test_zero_fill_clause_is_suppressed_not_printed_as_n_zero():
    """A window with no fills must print NOTHING, never 'n=0, +0.00 net'.

    'n=0' beside a lifetime net reads as 'it made nothing recently', which is a
    different claim from 'this window cannot be measured'.
    """
    recent = {"loose": {"n": 0, "net": 0.0, "days": 28, "anchored": True,
                        "end_utc": DEAD_LAST.isoformat()}}
    note = _note(EDGES, {"loose": DEAD_LAST.isoformat()},
                 stopped={"loose": {"source": "signals-state.json"}},
                 recent_edge_net=recent)
    assert "loose" in note, "the STOPPED line itself must still render"
    assert "n=0" not in note, "a zero-fill window was printed as a measurement"
    assert "to last fill" not in note


# ------------------------------------------------------------- the rendered line

def _note(edges, last_fill, stopped=None, recent_edge_net=None):
    meta = {"last_fill": last_fill, "stopped": stopped or {}}
    if recent_edge_net is not None:
        meta["recent_edge_net"] = recent_edge_net
    # `_suggest` short-circuits to `wait_for_data` on a thin book and never
    # reaches §4c, so the caller must pass a full-sized one. This is not
    # decoration: the 2026-11-08 tests called `_suggest` with an empty book and
    # passed for the wrong reason.
    paper = {"n": 3000, "win_rate": 42.4, "ev": -0.0025, "profit_factor": 0.92,
             "total_pnl": -7.24}
    out = il._suggest(paper, {}, [], edges, meta)
    for s in out:
        if s["type"] == "edge_not_live":
            return s["idea"]
    return ""


EDGES = [{"edge": "loose", "n": 1261, "win_rate": 42.8, "avg_pnl": 0.0027,
          "net_pnl": 3.3689, "gross_pnl": 12.79, "symbols": ["SUI/USDT"]}]


def test_line_renders_the_clause_end_to_end_from_a_real_computation():
    """The test the 2026-11-08 fix was missing: no hand-injected map."""
    book = _dead_book()
    anchors = {"loose": DEAD_LAST.isoformat()}
    recent = il._recent_edge_net(book, il.PF_WINDOW_DAYS, now=NOW, anchors=anchors)
    note = _note(EDGES, {"loose": DEAD_LAST.isoformat()},
                 stopped={"loose": {"source": "signals-state.json"}},
                 recent_edge_net=recent)
    assert "loose" in note
    assert "to last fill" in note, (
        "the clause vanished from the rendered line — this is the exact "
        "2026-11-09 regression, where the number was computed and then dropped"
    )
    assert re.search(r"n=5, -\d", note)


def test_anchored_line_never_claims_a_now_ending_window():
    """'last 28d' asserts a window ending today; an anchored one does not."""
    recent = {"loose": {"n": 5, "net": -0.5, "days": 28,
                        "anchored": True, "end_utc": DEAD_LAST.isoformat()}}
    note = _note(EDGES, {"loose": DEAD_LAST.isoformat()},
                 stopped={"loose": {"source": "signals-state.json"}},
                 recent_edge_net=recent)
    line = [p for p in re.split(r";\s*(?=[a-z]+ \()", note) if "loose" in p][0]
    assert "last 28d" not in line, (
        "an anchored window must not be described as 'last 28d' — that is a "
        "claim about a window ending at report time"
    )
    assert "28d to last fill" in line


def test_live_edge_keeps_the_familiar_wording():
    recent = {"validated": {"n": 9, "net": 0.4, "days": 28, "anchored": False}}
    meta = {"last_fill": {"validated": NOW.isoformat()}, "stopped": {},
            "recent_edge_net": recent}
    rows = [{"edge": "validated", "n": 1400, "win_rate": 44.0, "avg_pnl": -0.003,
             "net_pnl": 9.0, "gross_pnl": 12.0, "symbols": ["SUI/USDT"]}]
    note = ""
    for s in il._suggest({}, {}, [], rows, meta):
        if s["type"] == "edge_not_live":
            note = s["idea"]
    if note:
        assert "last 28d" in note


def test_analyze_anchors_dead_edges_at_their_last_fill():
    """Static guard: analyze() must pass anchors, not call the bare helper."""
    src = (ROOT / "scripts" / "improvement_loop.py").read_text()
    m = re.search(r'edge_meta\["recent_edge_net"\]\s*=\s*_recent_edge_net\((.*?)\)',
                  src, re.S)
    assert m, "analyze() no longer assigns recent_edge_net"
    assert "anchors=" in m.group(1), (
        "analyze() calls _recent_edge_net without anchors — that is the "
        "2026-11-09 bug and it makes every STOPPED clause unreachable"
    )
