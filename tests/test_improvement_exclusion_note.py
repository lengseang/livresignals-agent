"""A per-edge verdict is about an edge that is still ALLOWED to trade.

Context (2026-11-08). `paper_book.edge_liveness` stamps the last fill of every
edge the book has ever held, and both readers treat "filled inside 14 days" as
LIVE. On 2026-11-08 the `loose` opener last filled 2026-10-11 — four weeks
earlier — so the report's rule 3 split the book into:

    live  validated  n=1386  -5.34 net  PF 0.87
    dead  loose      n=1261  +3.41 net

and then emitted the `edge_not_live` history note over exactly those same two
numbers, because that note is built from EVERY edge in `by_edge` (dead or
alive) and never from the split that produced the verdict. So one report said,
in two adjacent bullets:

    "profit factor 0.87 ... on the live edges (validated) ... NOT counted:
     1517 trades from 8 edge(s) with no fill in >14d (-2.17 net)"
    "NOT LIVE — no per-edge verdict issued (no fill in >14d): ...
     validated (... -5.34 net); loose (... +3.41 net)"

`loose` had a 0-day-old verdict in bullet one and a ">14d, no verdict issued"
label in bullet two. Two consequences, both defects:

  * the reader cannot tell which edges the split actually excluded — the note
    named the one edge the verdict WAS about;
  * `loose` is not dead at all. Measured on the live book the same day: the
    ledger is 100% `validated` for the last 300 fills (last `loose` fill
    2026-10-11), so the book's only net-POSITIVE cohort sits in the history
    bucket while a one-line reading of the note says its design was abandoned.

The fix is wording + provenance only: the note is computed from the SPLIT's
dead set (not from an independent re-derivation of ">14d"), the cohort line
names all live edges (not just those with a non-null PF), and an edge the
writer has declared stopped (`signals-state.json` `disabled`) is named as
STOPPED IN THE CONFIG rather than "no fill in >14d" — the two have different
next actions, and the existing wording conflated them.

What these tests pin, through `_suggest` and `_split_live_pf`:
  * the note names exactly the edges the split excluded,
  * an edge that carries the aggregate verdict is never called NOT LIVE,
  * the cohort line names every live edge, PF or no PF,
  * a config-disabled edge is named as such (with the file it came from),
  * a stale-but-fillable edge keeps the "history, re-enable to re-test" wording,
  * no note is emitted when nothing was excluded.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _edge_row(name, n=30, net=-1.0, wr=30.0, win_usd=None, loss_usd=None):
    """An edge row as `analyze()` builds it, with real win/loss dollars so the
    split's PF is exact (a net-sum-only row is flagged inexact by design)."""
    if win_usd is None or loss_usd is None:
        win_usd, loss_usd = (abs(net) * 0.8, abs(net) * 0.8 + abs(net)) \
            if net < 0 else (abs(net) * 1.8, abs(net) * 0.8)
    return {"edge": name, "n": n, "win_rate": wr, "win_rate_gross": wr,
            "avg_pnl": net / n, "net_pnl": net, "gross_pnl": net,
            "gross_win": win_usd, "gross_loss": loss_usd,
            "symbols": ["BTC/USDT"]}


def _paper(n=100, wr=42.0):
    return {"n": n, "win_rate": wr, "win_rate_gross": wr + 4, "ev": -0.001,
            "ev_gross": 0.004, "profit_factor": 0.95, "total_pnl": -1.0,
            "gross_pnl": 3.0, "uncosted": 0}


def _suggest(edges, last_fill, redundant=None, stopped=None, recent=None):
    il = _load("improvement_loop")
    live = {"n": 20, "win_rate": 42.0, "win_rate_gross": 42.0, "ev": -0.001,
            "ev_gross": -0.001, "profit_factor": 0.8, "total_pnl": -0.2,
            "gross_pnl": -0.2, "uncosted": 0}
    meta = {"stale": False, "last_fill": last_fill,
            "redundant": redundant or {}, "stopped": stopped or {}}
    if recent is not None:
        meta["recent_pf"] = recent
    else:
        meta["recent_pf"] = il._recent_live_pf([], meta)  # no trades: no vote
    return il._suggest(_paper(), live, [], edges, meta)


def _suggest_with_exclusions(edges, last_fill, dead_names):
    """Same call, but the SPLIT's dead set is pinned to `dead_names`.

    `_split_live_pf` derives dead from `_edge_is_live(last_fill)`, so a naive
    fixture makes "the split's dead set" and "n>=10 and not live" agree by
    construction — and a note re-derived from the second rule passes. This is
    what a real disagreement looks like to the note (e.g. the split sees a set
    the liveness map alone does not produce), and it is reproducible only
    because the test names which edges the split excluded.
    """
    il = _load("improvement_loop")
    real_split = il._split_live_pf

    def _pf_of(sub):
        gw = sum(r.get("gross_win", 0) or 0 for r in sub)
        gl = sum(r.get("gross_loss", 0) or 0 for r in sub)
        pf = (gw / gl) if gl > 0 else (None if gw > 0 else 0.0)
        return {"n": sum(r["n"] for r in sub),
                "net": round(sum(r["net_pnl"] for r in sub), 4),
                "pf": round(pf, 2) if pf is not None else None,
                "exact": True, "edges": sorted(r["edge"] for r in sub)}

    def _returning(dead_names_binding):
        def _pinned(rows, meta):
            dead, live_rows = [], []
            for r in rows:
                (dead if r["edge"] in dead_names_binding else live_rows).append(r)
            return {"live": _pf_of(live_rows),
                    "dead": _pf_of(dead) if dead
                            else {"n": 0, "net": 0.0, "pf": None,
                                  "exact": True, "edges": []},
                    "whole": _pf_of(rows),
                    "has_liveness": True}
        return _pinned

    il._split_live_pf = _returning(dead_names)  # type: ignore[attr-defined]
    try:
        # Call the module instance we patched. `_load()` re-executes the file
        # into a NEW module object each time, so the module-level `_suggest`
        # the test would otherwise reach is a different one (whose global
        # `_split_live_pf` is the real function) — the first draft of this
        # helper patched a module nobody called and the test silently failed.
        return il._suggest(_paper(), _paper(), [], edges,
                           {"stale": False, "last_fill": last_fill,
                            "redundant": {}, "stopped": {},
                            "recent_pf": {"available": False, "reason": "test"}})
    finally:
        il._split_live_pf = real_split  # type: ignore[attr-defined]


def test_note_is_computed_from_the_split_not_re_derived():
    """The split excludes `loose` (of 1261 trades). The liveness map handed to
    `_suggest` still says loose filled 2 days ago, i.e. a re-derivation with
    `_edge_is_live` would call it LIVE and never name it. The note must follow
    the split — the same set the aggregate verdict was computed on."""
    edges = [_edge_row("validated", n=1386, net=-5.34, wr=43.8),
             _edge_row("loose", n=1261, net=+3.41, wr=44.0,
                       win_usd=8.0, loss_usd=4.0)]
    fills = {"validated": _ago(0.02), "loose": _ago(2)}  # loose LOOKS live
    out = _suggest_with_exclusions(edges, fills, dead_names={"loose"})
    note = _notes(out)
    assert note and "loose" in note[0] and "validated" not in note[0]
    assert "validated" in _rr(out)[0]
    # A line 2 days old under a ">14d" header is the same defect class: the
    # stated reason must be one that is true of every line under it.
    assert ">14d" not in note[0], (
        "the note inherited a '>14d' claim while printing last fill "
        f"{_ago(2)[:10]}")
    assert _ago(2)[:10] in note[0]
    # control: with no disagreement, the same fixture names nobody
    out2 = _suggest_with_exclusions(edges, fills, dead_names=set())
    assert not _notes(out2)


def _notes(out):
    return [s["idea"] for s in out if s["type"] == "edge_not_live"]


def _rr(out):
    return [s["idea"] for s in out if s["type"] == "rr_tune"]


def test_the_measured_2026_11_08_report_stops_contradicting_itself():
    """The exact shape of the live card: validated live-and-losing, loose dead."""
    out = _suggest(
        [_edge_row("validated", n=1386, net=-5.34, wr=43.8),
         _edge_row("loose", n=1261, net=+3.41, wr=44.0)],
        {"validated": _ago(0.02), "loose": _ago(28)})
    rr = _rr(out)
    notes = _notes(out)
    assert len(rr) == 1, "the live cohort must still draw its net-PF verdict"
    assert len(notes) == 1

    # The verdict is about validated only; the note must not deny that.
    assert "validated" in rr[0]
    assert "validated" not in notes[0], (
        "the edge the aggregate verdict was drawn on must not be listed as "
        "NOT LIVE in the same report")
    # loose was excluded, so the note must name it.
    assert "loose" in notes[0] and "+3.41" in notes[0]
    # ...and the exclusion must be consistent with the split's own count.
    assert "1261 trades" in rr[0], (
        "the 'NOT counted' count in the verdict and the note's population "
        "must be the same split")


def test_note_membership_equals_the_split_even_at_the_edges_of_it():
    """Mutation surface: any note built from a rule OTHER than the split's
    dead set drifts here. `pullback` (n=12, dead) is below the quoted
    materiality floor; `validated` is live; `loose` is dead and stopped."""
    edges = [_edge_row("validated", n=1386, net=-5.34, wr=43.8),
             _edge_row("loose", n=1261, net=+3.41, wr=44.0),
             _edge_row("pullback", n=12, net=-1.1)]
    fills = {"validated": _ago(0.02), "loose": _ago(28), "pullback": _ago(50)}
    out = _suggest(edges, fills, stopped={"loose": {"source": "signals-state.json"}})
    note = _notes(out)[0]
    assert "pullback" in note and "loose" in note
    assert "validated" not in note
    rr = _rr(out)[0]
    assert "validated" in rr
    assert "1273 trades" in rr, "1386 live + 1261 loose + 12 pullback = 2659; "\
        "NOT counted is 1261+12"


def test_note_names_exactly_the_split_dead_set():
    """Three dead, one live: the note's membership IS the excluded set.

    The expected membership is spelled out as a literal rather than read back
    from `_split_live_pf` — a test that asks the production function for its
    own answer and then compares cannot fail when that function is wrong.
    """
    edges = [_edge_row("validated", net=-2.0),
             _edge_row("supertrend", net=-1.9),
             _edge_row("bollinger", net=-1.0),
             _edge_row("loose", net=+3.0, wr=44.0)]
    fills = {"validated": _ago(0.1), "supertrend": _ago(40),
             "bollinger": _ago(60), "loose": _ago(30)}
    out = _suggest(edges, fills)
    note = _notes(out)[0]
    for dead in ("supertrend", "bollinger", "loose"):
        assert dead in note
    assert "validated" not in note
    il = _load("improvement_loop")
    split = il._split_live_pf(edges, {"last_fill": fills})
    assert split["live"]["edges"] == ["validated"], \
        "the live side must be the one edge the note does not name"


def test_cohort_line_names_a_live_edge_with_no_pf():
    """A live edge can have PF None (no losing dollars this window). It is
    still live, and the cohort sentence naming only PF-bearing edges reads as
    if it is dead while the next bullet calls it NOT LIVE.

    Pinned through the WINDOWED branch (a recent vote) because that is where
    only the cohort is named and the pooled figure — the one whose PF may be
    None — is held back, so `pop["edges"]` must still carry both edges."""
    edges = [_edge_row("validated", n=600, net=-5.34, wr=43.8),
             _edge_row("loose", n=40, net=+0.5, wr=60.0,
                       win_usd=0.5, loss_usd=0.0)]
    recent = {"available": True, "days": 28, "n": 300, "net": -2.0, "pf": 0.70,
              "undated": 0, "start_utc": _ago(28)}
    out = _suggest(edges, {"validated": _ago(0.1), "loose": _ago(1)},
                   recent=recent)
    rr = _rr(out)
    assert rr, "the window is under the floor: the cohort verdict must fire"
    assert "loose" in rr[0] and "validated" in rr[0], (
        "both live edges are in the cohort; a sentence that omits the PF-less "
        "one makes the next bullet ('loose: NOT LIVE') look consistent")
    assert not _notes(out), "nothing was excluded, so nothing may be listed"


def test_no_note_when_nothing_was_excluded():
    out = _suggest([_edge_row("validated", net=-2.0)],
                   {"validated": _ago(0.1)})
    assert not _notes(out)


def test_a_dead_edge_too_small_to_exclude_is_still_named():
    """The split only *states* its dead cohort when n>=20 (materiality). The
    note is about what the split did EVERY time — an edge left out of the
    verdict must be named even if its 12 trades were too few to quote.

    Built through the pinned-split helper so the `n>=10` floor is the ONLY
    difference between the two candidate rule sets: a naive fixture has no
    dead row under 10 to drop, which is why the first draft of this test let
    the floor mutation through."""
    edges = [_edge_row("validated", n=1386, net=-5.34, wr=43.8),
             _edge_row("pullback", n=9, net=-1.1)]
    fills = {"validated": _ago(0.1), "pullback": _ago(50)}
    out = _suggest_with_exclusions(edges, fills, dead_names={"pullback"})
    note = _notes(out)
    assert note and "pullback" in note[0] and "n=9" in note[0]
    assert "validated" not in note[0]


def test_a_split_excluding_nothing_draws_no_exclusion_note():
    """The note is a statement ABOUT the split's dead set. If the split
    excluded nobody, there is nothing to explain — an independently-derived
    note would still fire here on a 50-day-old edge, i.e. it would announce an
    exclusion that the verdict above did not make."""
    edges = [_edge_row("validated", n=1386, net=-5.34, wr=43.8),
             _edge_row("pullback", n=17, net=-1.1)]
    fills = {"validated": _ago(0.1), "pullback": _ago(50)}
    out = _suggest_with_exclusions(edges, fills, dead_names=set())
    assert not _notes(out)
    assert "validated" in _rr(out)[0]


def test_undateable_edge_is_never_listed_as_dead():
    """Read-side precision rule (#14) is unchanged: an edge whose stamp cannot
    be parsed stays live, so it must not appear in the exclusion note."""
    out = _suggest([_edge_row("validated", net=-2.0),
                    _edge_row("mystery", net=-3.0)],
                   {"validated": _ago(0.1), "mystery": "not-a-timestamp"})
    assert not [n for n in _notes(out) if "mystery" in n]


# ------------------------------------------------- stopped vs merely stale
def test_config_disabled_edge_is_named_as_stopped_not_stale():
    """`loose` stopped filling 2026-10-11 and signals-state.json disables it.
    The next action differs from 'waiting for a fill': one is a decision
    already taken, the other is a question. Name the one that is true."""
    out = _suggest(
        [_edge_row("validated", net=-2.0), _edge_row("loose", net=+3.4)],
        {"validated": _ago(0.1), "loose": _ago(28)},
        stopped={"loose": {"since": "2026-10-11", "source": "signals-state.json"}})
    note = _notes(out)[0]
    assert "loose" in note
    assert "STOPPED" in note and "signals-state.json" in note
    assert "Re-enable ONE at a time" in note, (
        "a stopped edge's re-test instruction is the same as a stale one's")


def test_stale_but_fillable_edge_keeps_history_wording():
    out = _suggest([_edge_row("bollinger", net=-1.0)],
                   {"bollinger": _ago(45)})
    note = _notes(out)[0]
    assert "STOPPED" not in note, "not every dead edge was a decision"
    assert "HISTORY" in note and _ago(45)[:10] in note


def test_stopped_field_is_optional_and_degrades_silently():
    """An older caller / a broken state read must not crash the loop and must
    not invent a STOPPED label."""
    out = _suggest([_edge_row("loose", n=40, net=+1.0)],
                   {"loose": _ago(30)})
    note = _notes(out)[0]
    assert "STOPPED" not in note and "HISTORY" in note


def test_loader_reads_the_writers_disable_list_without_inventing_a_date():
    """`_stopped_edges` is the only bridge to signals-state.json. Bare list,
    no date in the file -> `since` is None, never "today"."""
    import json as _json
    import tempfile
    il = _load("improvement_loop")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "signals-state.json"
        p.write_text(_json.dumps({"disabled": ["loose", "rsi2", ""],
                                  "disabled_symbols": ["SOL/USDT"]}))
        got = il._stopped_edges(p)
        assert set(got) == {"loose", "rsi2"}, "only `disabled`, not symbols"
        assert got["loose"]["since"] is None
        assert got["loose"]["source"] == "signals-state.json"
        # end-to-end: the loaded stamp reaches the note as STOPPED
        out = _suggest([_edge_row("loose", n=40, net=+1.0)],
                       {"loose": _ago(30)}, stopped=got)
        assert "STOPPED in signals-state.json" in _notes(out)[0]
        # missing file / corrupt file degrade to the old wording, not to a lie
        assert il._stopped_edges(Path(td) / "nope.json") == {}
        p.write_text("{not json")
        assert il._stopped_edges(p) == {}


def test_analyze_loads_the_disable_list_into_edge_meta():
    """Wiring, not wording: `_stopped_edges` is worthless if `analyze()` never
    calls it, or calls it on a file no writer maintains.

    A source-text check for the bare name is not enough — the stale
    `signals-state.json.bak-20261014T130253Z` sibling sits in the same
    directory and would satisfy `'signals-state.json' in src` as a substring.
    So the path is pinned literally and the literal is checked to be its own
    string token.
    """
    import inspect
    import re
    il = _load("improvement_loop")
    src = inspect.getsource(il.analyze)
    m = re.search(r'edge_meta\["stopped"\]\s*=\s*_stopped_edges\(([^)]*)\)', src)
    assert m, "analyze must hand the writer's disable list to _suggest"
    arg = m.group(1)
    assert arg == 'DATA / "signals-state.json"', (
        f"the disable list must come from the writer's live file, got {arg!r}")
