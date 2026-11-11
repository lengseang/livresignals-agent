"""T0.21 — the p1 win-rate line must ask a cohort that can still be tuned.

THE DEFECT (measured 2026-11-11 on `reports/improvement-report.json`)

Rule 2 of `improvement_loop._suggest` read `paper` — the WHOLE collapsed book —
and said, on every run for weeks, at priority 1:

    "Paper win rate 42.9% below 55% — the capitulation edge may be decaying.
     Trigger a walk-forward re-fit and consider tightening RSI or vol
     thresholds."

Three separate things were wrong, and the tests below pin one each:

1. POPULATION. 42.9% is the whole book (n=2999). 1517 of those fills belong to
   eight edges that cannot fill (seven STOPPED in `signals-state.json`, plus
   `loose`, idle since 2026-10-11). The one LIVE edge, `validated`, reads 45.1%
   over n=1482. A verdict computed over corpses cannot be acted on; every rule
   beside it (#14, #15, #16) had already been taught this, and this was the last
   one still pooling them.

2. BASES. The 55% bar is a GROSS bar: it was written 2026-09-21, three weeks
   before the book switched to NET on 2026-10-11 (the fee it charged was 5.4x
   too small). Measured the same day, whole book 47.8% gross but 42.9% net;
   `validated` 49.5% gross / 45.1% net. The fixture below shows the consequence
   with nothing else varying: a book whose gross rate clears 55% while its net
   rate does not must NOT be called decaying — it pays its costs.

3. MANDATE. "Trigger a walk-forward re-fit" named no runner. The sentence now
   names where the bar is re-derived and says what the reader can run.

And one thing the line must NOT do: a cohort that PAYS (net) at a low win rate
is left alone — the same rule the per-edge demote guard follows since
2026-10-18. `loose` at 48.8% / +$4.38 was "left alone" then; the book-level
line must not re-flag it under a different name.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _live_meta(rows, edges):
    """Edge meta shaped exactly as `analyze()` now emits it for the live split."""
    il = _load("improvement_loop")
    meta = {"last_fill": {e: "2026-11-11T09:00:00+00:00" for e in edges},
            "redundant": {}}
    names = set(edges)
    pop = il.stats_of([r for r in rows if r["edge"] in names])
    pop["name"] = "live"
    pop["edges"] = sorted(names)
    meta["win_rate_population"] = pop
    return il, meta


def _row(edge, n, win_rate, net_pnl, gw, gl):
    return {"edge": edge, "n": n, "win_rate": win_rate, "win_rate_gross": None,
            "avg_pnl": net_pnl / n, "net_pnl": net_pnl, "gross_pnl": net_pnl,
            "gross_win": gw, "gross_loss": gl, "symbols": ["BTC/USDT"]}


def _wr_lines(sugs):
    return [s for s in sugs if s["type"] == "edge_decay"
            and s["idea"].startswith("COHORT win rate")]


def test_the_line_reads_the_live_edge_not_the_stopped_eight():
    """THE REAL SHAPE (2026-11-11). One live edge at 45.1% over n=1482; seven
    stopped edges at ~25-35% over 1536 fills. The whole book reads 42.9%, the
    live cohort 45.1%.

    ⚠️ REWRITTEN 2026-11-11 (second shift). This test used to assert that a p1
    "COHORT win rate" paragraph existed for this shape. It does exist — but so
    does rule 4b's per-edge verdict over the SAME rows, and the two printed the
    same advice twice. A cohort of ONE is not a cohort: the per-edge line owns
    it. What must hold for this shape is (a) the population is the live edge and
    not the corpse-weighted book, (b) exactly ONE edge_decay line is emitted,
    and (c) its numbers are the live edge's.
    """
    live = [_row("validated", 1482, 45.1, -3.65, 40.18, 43.83)]
    dead = [_row("supertrend", 121, 30.6, -1.88, 2.64, 4.52),
            _row("bollinger", 45, 33.3, -1.00, 0.64, 1.65),
            _row("loose", 1261, 42.8, 3.46, 44.58, 41.12),
            _row("momentum", 27, 29.6, -0.15, 0.66, 0.82),
            _row("ema-cross", 22, 27.3, -0.83, 0.09, 0.92),
            _row("pullback", 17, 29.4, -1.11, 0.14, 1.24),
            _row("breakout", 17, 29.4, -0.36, 0.33, 0.69),
            _row("rsi2", 7, 14.3, -0.24, 0.12, 0.35)]
    rows = live + dead
    meta = {"last_fill": {"validated": "2026-11-11T09:00:00+00:00",
                          "loose": "2026-10-11T23:16:00+00:00"},
            "redundant": {}}
    il = _load("improvement_loop")
    for e in dead:
        if e["edge"] != "loose":
            meta["last_fill"][e["edge"]] = "2026-09-08T00:00:00+00:00"
    split = il._split_live_pf(rows, meta)
    assert split["live"]["edges"] == ["validated"], split["live"]
    pop = il.stats_of([r for r in rows if r["edge"] == "validated"])
    pop["name"] = "live"
    pop["edges"] = ["validated"]
    meta["win_rate_population"] = pop

    whole = {"n": sum(r["n"] for r in rows), "win_rate": 42.9}
    sugs = il._suggest(whole, None, [], rows, meta)
    decays = [s for s in sugs if s["type"] == "edge_decay"]
    assert len(decays) == 1, (
        "one live edge must produce exactly one verdict, not a cohort copy "
        f"plus an edge copy: {[d['idea'][:60] for d in decays]}")
    idea = decays[0]["idea"]
    assert idea.startswith("Edge 'validated'"), idea
    assert "45.1%" in idea, idea
    assert "1482" in idea, idea
    # The corpse-weighted 42.9% must never be the basis of a live verdict.
    assert not any("42.9%" in d["idea"] for d in decays), decays


def test_a_cohort_of_several_edges_still_gets_its_own_line():
    """The suppression is `n_edges == 1` ONLY. Two live, decaying edges are a
    cohort that no single per-edge line speaks for, so the p1 line must fire.

    This is the PASSING control for the test above: a guard that silences
    everything would look identical to the fix on the one-edge shape.
    """
    rows = [_row("validated", 800, 44.0, -3.00, 20.0, 23.0),
            _row("momentum", 600, 41.0, -2.00, 14.0, 16.0)]
    meta = {"last_fill": {"validated": "2026-11-11T09:00:00+00:00",
                          "momentum": "2026-11-11T08:00:00+00:00"},
            "redundant": {}}
    il = _load("improvement_loop")
    split = il._split_live_pf(rows, meta)
    assert len(split["live"]["edges"]) == 2, split["live"]
    pop = il.stats_of(rows)
    pop["name"] = "live"
    pop["edges"] = sorted(split["live"]["edges"])
    meta["win_rate_population"] = pop

    sugs = il._suggest({"n": 1400, "win_rate": 42.7}, None, [], rows, meta)
    lines = _wr_lines(sugs)
    assert len(lines) == 1, f"a two-edge cohort needs its own line: {sugs}"
    assert "n=1400" in lines[0]["idea"], lines[0]["idea"]
    assert "validated" in lines[0]["idea"] and "momentum" in lines[0]["idea"]


def test_a_book_that_pays_its_costs_is_not_called_decaying():
    """THE BASIS DEFECT. Nothing varies but the basis: gross 56.0% clears the
    55% bar, net 51.0% does not, and the cohort is net-POSITIVE. Judged on net
    the old rule fires; judged on the bar it was written on, it must not."""
    rows = [_row("validated", 1000, 51.0, 3.20, 44.0, 40.8)]
    il, meta = _live_meta(rows, ["validated"])
    # The trade-level population carries both bases, as analyze() emits them.
    meta["win_rate_population"] = {"n": 1000, "win_rate": 51.0,
                                   "win_rate_gross": 56.0,
                                   "total_pnl": 3.20, "profit_factor": 1.04}
    sugs = il._suggest({"n": 1000, "win_rate": 51.0}, None, [], rows, meta)
    assert not _wr_lines(sugs), (
        "a book whose gross rate clears 55% and which pays its costs was "
        f"called decaying: {_wr_lines(sugs)}")


def test_a_net_positive_low_win_rate_cohort_is_left_alone():
    """The 2026-10-18 rule, at book level: `loose` at 48.8% / +$4.38 is "left
    alone", so the book-level line must not re-flag it under another name."""
    rows = [_row("loose", 1376, 48.8, 4.38, 55.0, 50.6)]
    il, meta = _live_meta(rows, ["loose"])
    meta["win_rate_population"] = {"n": 1376, "win_rate": 48.8,
                                   "win_rate_gross": None, "total_pnl": 4.38}
    sugs = il._suggest({"n": 1376, "win_rate": 48.8}, None, [], rows, meta)
    assert not _wr_lines(sugs), f"a paying book was flagged: {_wr_lines(sugs)}"


def test_a_losing_book_below_the_bar_still_fires_and_names_its_basis():
    """The guard must not cry wolf, but it must still bite a loser."""
    rows = [_row("validated", 800, 44.0, -3.00, 20.0, 23.0)]
    il, meta = _live_meta(rows, ["validated"])
    meta["win_rate_population"] = {"n": 800, "win_rate": 44.0,
                                   "win_rate_gross": 49.0,
                                   "total_pnl": -3.00, "profit_factor": 0.87}
    sugs = il._suggest({"n": 800, "win_rate": 44.0}, None, [], rows, meta)
    lines = _wr_lines(sugs)
    assert len(lines) == 1, sugs
    idea = lines[0]["idea"]
    assert "44.0%" in idea, "the judged (net) rate must be the one quoted"
    assert "NET" in idea, "a net number must say it is net"
    assert "GROSS" in idea, "the bar's basis must be stated"
    assert "walk-forward" in idea, "the re-derivation must be named"


def test_no_liveness_map_degrades_to_the_whole_book():
    """An older caller passes no `win_rate_population`. Rule 2 must still run
    — on the whole book, exactly as before T0.21 — and must not crash."""
    il = _load("improvement_loop")
    rows = [_row("validated", 100, 40.0, -1.0, 10.0, 11.0)]
    sugs = il._suggest({"n": 100, "win_rate": 40.0,
                        "win_rate_gross": 46.0}, None, [], rows, None)
    lines = _wr_lines(sugs)
    assert len(lines) == 1, f"the degraded path must still warn: {sugs}"
    assert "whole book" in lines[0]["idea"]
    assert "n=100" in lines[0]["idea"]


def test_the_stats_reader_reproduces_a_population_from_edge_rows():
    """`_stats` must give the same answer for a set of edge rows as for the
    fills they summarise — otherwise the new line quotes a number that does
    not exist in the report it claims to read. `analyze()` uses the same
    function for both, so this pins the arithmetic once."""
    il = _load("improvement_loop")
    rows = [_row("a", 300, 40.0, -1.00, 12.0, 13.0),
            _row("b", 100, 60.0, 2.50, 4.0, 1.5)]
    s = il.stats_of(rows)
    assert s["n"] == 400
    assert s["win_rate"] == 45.0                     # (120 + 60) / 400
    assert abs(s["total_pnl"] - 1.50) < 1e-9
    assert s["profit_factor"] == round(16.0 / 14.5, 2)
    assert s["uncosted"] == 0


def test_an_unnamed_row_is_refused_not_read_as_a_zero_dollar_win():
    """A dict with no `pnl` and no `net_pnl`/`n` is neither a trade nor an edge
    row. Read as a trade it would be a $0 net win — a fabricated win rate out
    of a missing field."""
    il = _load("improvement_loop")
    try:
        il.stats_of([{"edge": "x"}])
    except ValueError:
        return
    raise AssertionError("a row with no fill basis must be refused")
