"""Rule 3 asked the DEAD half of the book how to tune the LIVE half's exits.

Context (2026-10-22): `improvement_loop._suggest` rule 3 emitted, at priority 2,
on every run:

    Profit factor 0.97 < 1.5. Consider raising TP:SL ratio (2:1 -> 2.5:1)
    or tightening the stop.

Measured on the collapsed book that same hour:

    whole book   n=2637   PF 0.97   net -2.24   gross-loss 85.11
    LIVE edges   n=2365   PF 1.05   net +3.44   gross-loss 74.43
      -> validated, loose
    DEAD edges   n= 272   PF 0.47   net -5.68   gross-loss 10.68
      -> supertrend, bollinger, momentum, ema-cross, breakout, pullback, rsi2

Seven of the nine edges had no fill for 38-46 days — six of them explicitly
named in the SAME report's `edge_not_live` note as "HISTORY, not the live
book". Their 272 trades lost $5.68 and dragged a live book that is net
POSITIVE (+$3.44) down to PF 0.97 — below 1.0 — and the advice was drawn from
that below-1.0 figure. This is the 2026-10-21 liveness rule (#14) applied to
the last place that still pooled the whole book: a rule's own premise computed
over a population the rules themselves had just declared untradeable.

Second defect in the same line: `profit_factor` is a NET ratio while its
threshold (1.5) was written for a GROSS book. At a measured 0.16% round trip PF
falls by roughly a third, so the bar was unreachable — an always-on warning,
the failure mode the 2026-10-13 tag-freshness fix removed.

Third: `rr_tune` is consumed by nothing (grep finds only producer and report)
and the paper openers take TP:SL from the design, not from a knob this loop
can move.

What these tests pin:
  * a book run entirely by live edges draws NO exit advice,
  * a live book that really is thin still draws it (not a mute button),
  * the advice states which edges it is about — by name and count,
  * the threshold is applied to the same population the figure describes,
  * a book with no liveness data degrades to the old whole-book reading,
  * a tiny live sample cannot carry the verdict.
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


def _edge_row(name, n, net, wr, win_usd=None, loss_usd=None):
    """An edge row as `analyze()` builds it, plus the win/loss dollar split
    that a real profit factor needs (net alone cannot reconstruct a ratio)."""
    w = int(round(n * wr / 100.0))
    l = n - w
    if win_usd is None or loss_usd is None:
        # 2:1 design, worked backwards from net = win_usd - loss_usd
        win_usd = max(net, 0) + 3.0 * max(n - w, 1) * 0.6
        loss_usd = win_usd - net
    return {"edge": name, "n": n, "win_rate": wr, "win_rate_gross": wr,
            "avg_pnl": net / n, "net_pnl": net, "gross_pnl": net,
            "wins": w, "losses": l,
            "gross_win": win_usd, "gross_loss": loss_usd,
            "symbols": ["BTC/USDT"]}


def _paper(n=100, pf=0.95, net=-1.0):
    return {"n": n, "win_rate": 42.0, "win_rate_gross": 47.0, "ev": -0.001,
            "ev_gross": 0.004, "profit_factor": pf, "total_pnl": net,
            "gross_pnl": 3.0, "uncosted": 0}


def _suggest(edges, last_fill, paper=None):
    il = _load("improvement_loop")
    live = {"n": 20, "win_rate": 42.0, "win_rate_gross": 42.0, "ev": -0.001,
            "ev_gross": -0.001, "profit_factor": 0.8, "total_pnl": -0.2,
            "gross_pnl": -0.2, "uncosted": 0}
    meta = {"stale": False, "last_fill": last_fill, "redundant": {}}
    return il._suggest(paper or _paper(), live, [], edges, meta)


def _rr(out):
    return [s["idea"] for s in out if s["type"] == "rr_tune"]


# The measured 2026-10-22 book: two live edges, seven dead ones. Dollar figures
# are the real per-edge net win/loss totals from `analyze()`, so the fixture
# reproduces PF 1.05 live / 0.47 dead / 0.97 whole exactly.
_LIVE = [_edge_row("loose", 1376, +3.57, 43.2, 48.73, 45.16),
         _edge_row("validated", 989, -0.13, 43.9, 29.14, 29.27)]
_DEAD = [_edge_row("supertrend", 134, -1.85, 31.3, 3.03, 4.88),
         _edge_row("bollinger", 47, -1.04, 34.0, 0.64, 1.67),
         _edge_row("momentum", 27, -0.16, 29.6, 0.66, 0.82),
         _edge_row("ema-cross", 22, -0.83, 27.3, 0.09, 0.93),
         _edge_row("breakout", 18, -0.44, 27.8, 0.33, 0.77),
         _edge_row("pullback", 17, -1.11, 29.4, 0.13, 1.25),
         _edge_row("rsi2", 7, -0.24, 14.3, 0.12, 0.36)]
_LIVE_FILL = {"loose": _ago(11), "validated": _ago(0.1)}
_DEAD_FILL = {"supertrend": _ago(44), "bollinger": _ago(42),
              "momentum": _ago(39), "ema-cross": _ago(40),
              "breakout": _ago(39), "pullback": _ago(46), "rsi2": _ago(44)}
_ALL_FILL = {**_LIVE_FILL, **_DEAD_FILL}


def test_the_fixture_reproduces_the_measured_book():
    """Guard the fixture itself: if it stops matching 2026-10-22, the tests
    below are pinning a story rather than the defect."""
    il = _load("improvement_loop")
    assert il._pf_from_rows(_LIVE + _DEAD)["pf"] == 0.97
    assert il._pf_from_rows(_LIVE)["pf"] == 1.05
    assert il._pf_from_rows(_DEAD)["pf"] == 0.47
    assert il._pf_from_rows(_LIVE + _DEAD)["n"] == 2637
    assert il._pf_from_rows(_LIVE)["n"] == 2365
    assert il._pf_from_rows(_DEAD)["n"] == 272


def test_dead_edges_do_not_drag_the_live_book_below_the_bar():
    """The measured defect: whole-book PF 0.97 was a verdict about seven corpses.

    `_paper(pf=0.97, net=-2.24)` is the whole-book figure the old rule quoted.
    The live book pays, so the advice must not appear at all.
    """
    out = _suggest(_LIVE + _DEAD, _ALL_FILL, paper=_paper(pf=0.97, net=-2.24))
    assert not _rr(out), (
        "exit advice drawn from a profit factor that is only below the bar "
        "because seven disabled edges are inside it: " + repr(_rr(out)))


def test_live_pf_is_reported_beside_the_whole_book_figure():
    """The reader must be able to see the split, not just be denied the line."""
    il = _load("improvement_loop")
    rows = il._split_live_pf(_LIVE + _DEAD, _ALL_FILL)
    assert rows["live"]["edges"] == ["loose", "validated"]
    assert rows["dead"]["n"] == 272
    assert "supertrend" in rows["dead"]["edges"]
    assert rows["whole"]["n"] == 2637
    assert rows["has_liveness"] is True


def test_a_genuinely_thin_live_book_still_gets_advice():
    """The guard is a population filter, not a mute button."""
    # PF = 36.6/37.5 = 0.98 -> below break-even on the live edges
    thin = [_edge_row("loose", 1376, -1.00, 41.0, 36.60, 37.60),
            _edge_row("validated", 989, -2.00, 40.0, 30.00, 31.00)]
    out = _suggest(thin, _LIVE_FILL, paper=_paper(pf=0.90, net=-3.0))
    got = _rr(out)
    assert len(got) == 1
    assert "1.02" in got[0], "the line must state the bar it is judging against"


def test_advice_names_the_population_it_was_measured_on():
    """Same rule as #12/#14: a verdict must state its window AND its cohort."""
    # PF = 36.6/37.5 = 0.98 -> below break-even on the live edges
    thin = [_edge_row("loose", 1376, -1.00, 41.0, 36.60, 37.60),
            _edge_row("validated", 989, -2.00, 40.0, 30.00, 31.00)]
    out = _suggest(thin, _LIVE_FILL, paper=_paper(pf=0.90, net=-3.0))
    idea = _rr(out)[0]
    assert "loose" in idea and "validated" in idea
    assert "2 edge" in idea, "the cohort size must be stated"
    assert "live" in idea.lower()


def test_the_excluded_population_is_named_when_material():
    """A suppression must leave a trace: say what was left out and why."""
    # PF = 36.6/37.5 = 0.98 -> below break-even on the live edges
    thin = [_edge_row("loose", 1376, -1.00, 41.0, 36.60, 37.60),
            _edge_row("validated", 989, -2.00, 40.0, 30.00, 31.00)]
    out = _suggest(thin + _DEAD, {**_LIVE_FILL, **_DEAD_FILL},
                   paper=_paper(pf=0.90, net=-3.0))
    idea = _rr(out)[0]
    assert "NOT counted" in idea and "272" in idea
    assert ">14d" in idea


def test_threshold_is_net_based_not_a_gross_bar():
    """The 1.5 bar was calibrated on gross PF; net PF is structurally lower.

    A live book at NET PF 1.05 on a ~2:1 design is paying after costs and must
    not be told to widen its target on a bar it can never clear.
    """
    il = _load("improvement_loop")
    assert il.PF_NET_FLOOR < 1.5, (
        "the net threshold must be derived for a costed book, not inherited "
        "from the gross-era 1.5")
    assert 1.0 <= il.PF_NET_FLOOR <= 1.05, (
        "the bar must sit just above break-even: the measured live book pays "
        "at 1.05 and must not be flagged")
    out = _suggest(_LIVE, _LIVE_FILL, paper=_paper(pf=1.05, net=3.44))
    assert not _rr(out), "net PF 1.05 on a paying book is not a defect"


def test_short_live_sample_does_not_issue_exit_advice():
    """A 15-trade live book cannot carry a PF verdict."""
    out = _suggest([_edge_row("validated", 15, -0.5, 40.0)],
                   {"validated": _ago(0.1)}, paper=_paper(n=15, pf=0.7))
    assert not _rr(out)


def test_no_liveness_data_degrades_to_the_whole_book():
    """A broken reader must not silence rule 3 — old behaviour, said out loud."""
    out = _suggest([_edge_row("loose", 1376, -4.0, 40.0)],
                   {}, paper=_paper(pf=0.80, net=-6.0))
    got = _rr(out)
    assert len(got) == 1
    assert "no liveness data" in got[0], "the degraded basis must be stated"


def test_an_all_winning_book_is_not_flagged():
    """No losing dollars => PF is undefined (infinite), never 0.00."""
    out = _suggest([_edge_row("loose", 100, +5.0, 90.0, 5.0, 0.0)],
                   {"loose": _ago(1)}, paper=_paper(pf=0.0, net=5.0))
    assert not _rr(out)
