"""The REDUNDANT note named pairs that no shared signal put together.

Context (2026-11-10, T0.18). `improvement_loop`'s REDUNDANT line shipped
`breakout ~ loose; momentum ~ loose; rsi2 ~ supertrend` as "copy of another
edge's handoff". Measured on the collapsed live book:

    `breakout`'s last fill is 2026-09-13; `loose`'s is 2026-10-11.
    The gap is 28 DAYS. `_shares_handoff` allows 72 HOURS.
    `momentum` (last fill 2026-09-13) vs `loose`: same 28-day gap.

So two of the three pairs could not have passed the predicate that is supposed
to create them. A pair in a published sentence must be a pair the predicate
actually produced.

WHY BOTH SIDES OF THE LINE ARE TESTED
-------------------------------------
It is tempting to fix only "stop printing unproven pairs" — but that would
also delete `rsi2 ~ supertrend` if the real link were accidental, and it is
not: all 7 surviving `rsi2` rows share a taught plan (same entry tick, same
TP/SL) with a `supertrend` row, which is one decision, whichever label the
writer stamped. So the test pins the DERIVATION on both sides:

  * a pair the predicate rejects is never NAMED (the observed defect), and
  * a pair the predicate accepts still is (the guard is not a mute button),
  * and it is recomputed from the book, not read off a constant — the loop's
    `edge_meta["redundant"]` must equal `edge_liveness(book)["redundant"]`.

Fixtures are hand-built so the arithmetic is inspectable; the last test runs
the REAL `analyze()` on the REAL book, because the defect lived in the gap
between a predicate and the sentence that quoted it.

The 2026-11-10 clause change lives in `scripts/paper_book._shares_handoff`:
same entry tick + same taught (tp, sl) plan = one handoff inside the 72h
window; same tick with a DIFFERENT plan = not a handoff; and two rows with no
taught plan are only a pair when they are seconds apart (the old 72h window
with no plan evidence is what let two designs that met a day apart at one
price read as copies). The cross-timeframe case stays exactly as it was: one
dip booked per timeframe is the multi-tf collapse's job, not redundancy's.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]   # tests/loop/ -> repo root
SCRIPTS = ROOT / "scripts"


def _load(name):
    """Same loader idiom as tests/loop/test_edge_symbol_concentration.py."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _row(symbol, tf, entry, ts, tp=None, sl=None, edge=None, pnl=0.01):
    return {"symbol": symbol, "tf": tf, "entry": entry, "tp": tp, "sl": sl,
            "ts_utc": ts, "edge": edge, "pnl": pnl, "size": 5}


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc).isoformat()


# ------------------------------------------------------------------ predicate
def test_same_tick_and_same_taught_plan_is_one_handoff_even_apart():
    pb = _load("paper_book")
    a = _row("XRP/USDT", "5m", 1.3983, _utc(2026, 9, 4, 21, 5, 13), 1.41, 1.39)
    b = _row("XRP/USDT", "5m", 1.3983, _utc(2026, 9, 5, 21, 5, 13), 1.41, 1.39)
    assert pb._shares_handoff(a, b)


def test_same_tick_different_taught_plan_is_not_one_handoff():
    """Two entries at one tick with different exits are two decisions."""
    pb = _load("paper_book")
    a = _row("XRP/USDT", "5m", 1.3983, _utc(2026, 9, 4, 21, 5, 13), 1.41, 1.39)
    b = _row("XRP/USDT", "5m", 1.3983, _utc(2026, 9, 4, 23, 5, 13), 1.45, 1.37)
    assert not pb._shares_handoff(a, b)


def test_same_tick_seconds_apart_without_plans_is_still_one_scan():
    """No plan evidence at all: the old second-scale rule still applies."""
    pb = _load("paper_book")
    a = _row("XRP/USDT", "5m", 1.3983, _utc(2026, 9, 4, 21, 5, 13))
    b = _row("XRP/USDT", "5m", 1.3983, _utc(2026, 9, 4, 21, 5, 15))
    assert pb._shares_handoff(a, b)


def test_same_tick_hours_apart_without_plans_is_not_claimed():
    """The observed defect's shape: no plan, far apart -> NOT a pair."""
    pb = _load("paper_book")
    a = _row("XRP/USDT", "5m", 1.3983, _utc(2026, 9, 4, 21, 5, 13))
    b = _row("XRP/USDT", "5m", 1.3983, _utc(2026, 9, 4, 23, 5, 13))
    assert not pb._shares_handoff(a, b)


def test_a_different_plan_hours_apart_is_not_a_pair():
    """M3 guard: the same tick with a DIFFERENT taught plan, hours later."""
    pb = _load("paper_book")
    a = _row("XRP/USDT", "5m", 1.3983, _utc(2026, 9, 4, 21, 5, 13), 1.41, 1.39)
    b = _row("XRP/USDT", "5m", 1.3983, _utc(2026, 9, 4, 23, 5, 13), 1.45, 1.37)
    assert not pb._shares_handoff(a, b)


def test_a_different_plan_reads_as_two_edges_not_a_copy():
    """M3, end to end: the same tick with different taught plans, hours
    apart, is never turned into a copy of one edge by another.

    The copy pays MORE per fill here, so even without the different-plan
    guard the "adds nothing" test would refuse it — which is why the direct
    predicate test above and the symmetric one below must both exist.
    """
    pb = _load("paper_book")
    rows = []
    for i in range(35):
        rows.append(_row("BTC/USDT", "5m", 100.0 + i,
                         _utc(2026, 10, 1, 0, 0, i), 101.0 + i, 99.0 + i,
                         edge="loose", pnl=0.10))
        rows.append(_row("BTC/USDT", "5m", 100.0 + i,
                         _utc(2026, 10, 1, 3, 0, i), 104.0 + i, 96.0 + i,
                         edge="momentum", pnl=0.01))
    liv = pb.edge_liveness(rows)
    assert "momentum" not in liv["redundant"], (
        "one tick, two different taught plans, three hours apart was read as "
        f"one handoff: {liv['redundant']}")


def test_a_worse_different_plan_is_not_the_better_one_s_copy():
    """M3 in both directions: the edge with the worse record is not a copy
    when the two were taught different exits hours apart."""
    pb = _load("paper_book")
    rows = []
    for i in range(35):
        rows.append(_row("BTC/USDT", "5m", 100.0 + i,
                         _utc(2026, 10, 1, 0, 0, i), 101.0 + i, 99.0 + i,
                         edge="loose", pnl=0.10))
        rows.append(_row("BTC/USDT", "5m", 100.0 + i,
                         _utc(2026, 10, 1, 3, 0, i), 104.0 + i, 96.0 + i,
                         edge="momentum", pnl=-0.01))
    liv = pb.edge_liveness(rows)
    assert "momentum" not in liv["redundant"], (
        "a different taught plan was read as a copy of the other edge: "
        f"{liv['redundant']}")


def test_the_plan_is_compared_as_a_pair_not_by_tp_alone():
    """M7 guard: tp equal, sl different, hours apart -> two plans."""
    pb = _load("paper_book")
    a = _row("XRP/USDT", "5m", 1.3983, _utc(2026, 9, 4, 21, 5, 13), 1.41, 1.39)
    b = _row("XRP/USDT", "5m", 1.3983, _utc(2026, 9, 4, 23, 5, 13), 1.41, 1.30)
    assert not pb._shares_handoff(a, b)


def test_the_window_bounds_the_plan_clause():
    """M4 guard: same tick AND same plan, four days apart -> not one signal."""
    pb = _load("paper_book")
    a = _row("XRP/USDT", "5m", 1.3983, _utc(2026, 9, 4, 21, 5, 13), 1.41, 1.39)
    b = _row("XRP/USDT", "5m", 1.3983, _utc(2026, 9, 8, 21, 5, 13), 1.41, 1.39)
    assert not pb._shares_handoff(a, b)


def test_cross_timeframe_is_left_to_the_multitf_collapse():
    """One dip booked per timeframe is not 'redundancy' — unchanged rule."""
    pb = _load("paper_book")
    a = _row("XRP/USDT", "5m", 1.3983, _utc(2026, 9, 4, 21, 5, 13), 1.41, 1.39)
    b = _row("XRP/USDT", "15m", 1.3983, _utc(2026, 9, 4, 21, 5, 13), 1.41, 1.39)
    assert not pb._shares_handoff(a, b)


# ------------------------------------------------------------- the sentence
def _book(*rows):
    """Collapsed-book shape `edge_liveness` expects (it re-derives the rest)."""
    return list(rows)


def test_a_pair_the_predicate_rejects_is_never_named():
    """The live defect: `breakout ~ loose`, 28 days apart, no shared plan."""
    pb = _load("paper_book")
    rows = []
    # incumbent: pays, 40 fills, recent
    for i in range(40):
        rows.append(_row("BTC/USDT", "5m", 100.0 + i * 0.5,
                         _utc(2026, 10, 1, 0, 0, i), 101.0 + i, 99.0 + i,
                         edge="loose", pnl=0.10))
    # the "copy": 31 fills, worse, on DIFFERENT ticks weeks later, no plan
    for i in range(31):
        rows.append(_row("BTC/USDT", "5m", 200.0 + i * 0.5,
                         _utc(2026, 9, 1, 0, 0, i), None, None,
                         edge="breakout", pnl=-0.05))
    liv = pb.edge_liveness(rows)
    assert "breakout" not in liv["redundant"], (
        "a pair sharing no signal was named a copy: "
        f"{liv['redundant']}")
    assert "breakout" not in pb.redundant_edges_text(liv)


def test_a_pair_the_predicate_accepts_is_still_named():
    """The guard is not a mute button: an accepted pair still prints."""
    pb = _load("paper_book")
    rows = []
    for i in range(40):
        rows.append(_row("BTC/USDT", "5m", 100.0 + i,
                         _utc(2026, 10, 1, 0, 0, i), 101.0 + i, 99.0 + i,
                         edge="loose", pnl=0.10))
    # the copy: same tick AND same taught plan as 31 of the parent's fills,
    # hours later, and it pays less per fill.
    for i in range(31):
        rows.append(_row("BTC/USDT", "5m", 100.0 + i,
                         _utc(2026, 10, 1, 6, 0, i), 101.0 + i, 99.0 + i,
                         edge="momentum", pnl=0.01))
    liv = pb.edge_liveness(rows)
    assert liv["redundant"].get("momentum") == "loose"
    # The PARENT is not a pair, and the copy is named once.
    text = pb.redundant_edges_text(liv)
    assert "'momentum' ~ copy of 'loose'" in text
    assert "'loose' ~ copy of" not in text


def test_regime_mismatch_still_blocks_a_pair():
    pb = _load("paper_book")
    a = _row("BTC/USDT", "5m", 100.0, _utc(2026, 10, 1, 0, 0, 0), 101, 99)
    b = _row("BTC/USDT", "5m", 100.0, _utc(2026, 10, 1, 0, 0, 0), 101, 99)
    a["regime"], b["regime"] = "RANGING", "TRENDING"
    assert not pb._shares_handoff(a, b)


# ------------------------------------------------------- the real path, real book
def test_the_loop_recomputes_the_pairs_it_prints():
    """`edge_meta['redundant']` must equal `edge_liveness(book)['redundant']`.

    Runs the REAL `analyze()` on the REAL `data/unified-paper.json`. If the
    book is missing (CI without data) the recomputation is skipped — the
    fixtures above still pin the predicate and the sentence.
    """
    book_path = ROOT / "data" / "unified-paper.json"
    if not book_path.exists():
        pytest.skip("no live paper book on this host")
    import json
    pb = _load("paper_book")
    il = _load("improvement_loop")
    raw = json.loads(book_path.read_text(encoding="utf-8")).get("closed", [])
    if not raw:
        pytest.skip("empty paper book")
    collapsed, _ = pb.collapse_multitf(raw)
    expected = pb.edge_liveness(collapsed)["redundant"]

    report = il.analyze()
    assert report["edge_meta"]["redundant"] == expected

    named = set()
    for s in report["suggestions"]:
        if s["type"] == "edge_redundant":
            for part in s["idea"].split(": ", 1)[1].split("; "):
                named.add(part.strip().split(" ~ ")[0])
    assert named == set(expected), (
        f"the REDUNDANT sentence names {sorted(named)}, the predicate "
        f"produced {sorted(expected)}")
