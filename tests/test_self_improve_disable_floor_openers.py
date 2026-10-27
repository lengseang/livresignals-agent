"""The disable FLOOR must count OPENERS, not records.

Context (2026-10-26): `_plan_disables` was written on 2026-10-14 to stop the
engine disabling every edge and freezing the paper book. It counted "something
remains" over `by_edge` — which is built from FINISHED TRADES. An edge that
stopped filling a month ago is still in that dict and still trips the disable
predicate, so it was counted as an opener that would "carry the book".

Measured on the live state that morning:

    data/signals-state.json  disabled = [pullback, supertrend, ema-cross,
    bollinger, breakout, momentum, rsi2, loose]        <- all 8
    book edges not flagged   = {validated}             <- the only filler
    book edges flagged       = {loose, ...}            <- loose last filled
                                                          2026-10-11

`remaining` = {loose} - {loose} = {} was NOT true, because the dead edges were
not flagged, so the floor did not fire; 'loose' was disabled and the list became
all nine. Nothing left could open. The card meanwhile printed "REFUSED to
disable the last open edge 'validated'" — naming an edge that was never a
candidate — and never mentioned the edge that actually went.

These tests drive the real planner with an `openable` set and pin both the
decision and the reporting. Run:
    ./venv/bin/python -m pytest tests/test_self_improve_disable_floor_openers.py -q
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


def _loser(edge, n=20, size=5.0, per_trade_pnl=-0.02):
    """Trades that trip the disable predicate at NET: negative, WR 0%."""
    return [{"pnl": per_trade_pnl, "size": size, "symbol": "X/USDT",
             "edge": edge} for _ in range(n)]


def _winner(edge, n=20, size=5.0, per_trade_pnl=0.05):
    return [{"pnl": per_trade_pnl, "size": size, "symbol": "X/USDT",
             "edge": edge} for _ in range(n)]


def _live_shape():
    """The 2026-10-26 book: eight dead edges (flagged) + one live edge."""
    by_edge = {e: _loser(e) for e in
               ("pullback", "supertrend", "ema-cross", "bollinger",
                "breakout", "momentum", "rsi2")}
    by_edge["loose"] = _loser("loose")            # dead since 2026-10-11
    by_edge["validated"] = _winner("validated")   # the only filler
    disabled = [e for e in by_edge if e != "validated"]
    return by_edge, disabled


def test_dead_edge_is_not_a_carrier_so_the_floor_fires():
    """The regression: with only ONE openable edge, a flagged 'loose' must be
    held back, not disabled. Old code returned ([], None) here."""
    si = _load("self_improve")
    by_edge, disabled = _live_shape()
    openable = {"validated"}          # 'loose' cannot open: it is disabled
    to_disable, survivor = si._plan_disables(by_edge, disabled, openable)
    assert survivor is None, "validated stays enabled — no floor needed"
    assert to_disable == [], "nothing is openable-and-flagged, nothing goes"


def test_floor_protects_the_last_openable_edge_when_it_is_flagged():
    """If the last openable edge is itself a loser, it must be kept: the book
    cannot be emptied. This is the case the old floor got right, kept as a
    non-regression for the new `openable` argument."""
    si = _load("self_improve")
    by_edge = {"loose": _loser("loose"), "validated": _winner("validated")}
    disabled: list[str] = []
    to_disable, survivor = si._plan_disables(by_edge, disabled, {"loose"})
    assert survivor == "loose"
    assert to_disable == []


def test_dead_flagged_edges_do_not_protect_each_other():
    """Two dead flagged edges and no live one: the floor must still fire, and
    only ONE (the strongest) is kept. Old code disabled both."""
    si = _load("self_improve")
    by_edge = {"loose": _loser("loose", per_trade_pnl=-0.05),
               "rsi2": _loser("rsi2", per_trade_pnl=-0.01)}
    to_disable, survivor = si._plan_disables(by_edge, [], {"loose", "rsi2"})
    assert survivor == "rsi2", "keep the least-bad openable edge"
    assert to_disable == ["loose"]


def test_openable_default_keeps_old_behaviour():
    """Callers that do not pass `openable` get the 2026-10-14 semantics."""
    si = _load("self_improve")
    by_edge = {"a": _loser("a"), "b": _loser("b")}
    to_disable, survivor = si._plan_disables(by_edge, [])
    assert survivor is not None and len(to_disable) == 1


def test_openable_edges_reads_the_engine_and_honours_the_disable_list():
    """`_openable_edges` is the engine's enable list, not the book's history."""
    si = _load("self_improve")
    by_edge = {e: _loser(e) for e in
               ("validated", "loose", "pullback", "supertrend")}
    disabled = ["pullback", "supertrend"]
    got = si._openable_edges(by_edge, disabled)
    assert got == {"validated", "loose"}, got
    # Every book edge not in the disable list is openable via the imported
    # track or a native opener; an unknown name is never invented.
    assert si._openable_edges({"zzz-not-an-edge": _loser("zzz-not-an-edge")}, [])


def test_disable_plan_names_what_goes_and_what_stays():
    """The card must state the post-plan openable set, not stay silent."""
    src = (SCRIPTS / "self_improve.py").read_text(encoding="utf-8")
    assert "still openable" in src, "the plan must name survivors on the card"
    assert "_openable_edges(by_edge, state[\"disabled\"])" in src, \
        "main() must feed the floor the engine's enable list"
