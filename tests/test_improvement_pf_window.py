"""The aggregate PF verdict must be measured on a WINDOW, not on a lifetime.

THE DEFECT (found 2026-10-23)
-----------------------------
Rule #15 taught rule 3 to ask only the LIVE edges. It still asked them about
their WHOLE LIFE. Measured on the real book that day:

    live cohort (loose, validated)  n=2183   net +2.20   PF 1.03
      2026-09  n=1267  net  +6.94  PF 1.17
      2026-10  n= 902  net  -4.81  PF 0.82
    weekly PF: 1.53, 1.27, 0.75, 0.75, 1.20, 0.50

1.03 clears PF_NET_FLOOR (1.02) by one basis point, so the loop drew NO exit
advice at all — while the last four weeks lost $6.26 in the 28d window and the
weekly series was still falling. The lifetime mean is dominated by a September
the book can no longer trade in.

That is rule #12 ("a verdict must state the window it was measured on") applied
to the last aggregate that had no window. The tests below pin the fix:

  1. the window is computed over LIVE edges only, inside the last N days;
  2. a book that pays over its life but loses in the window DOES draw advice;
  3. the advice names BOTH figures and the window it used;
  4. a book that pays in the window does NOT draw advice even if its life is bad;
  5. a thin window cannot manufacture a verdict;
  6. an undated record cannot vote on a window;
  7. absent window data degrades to the pre-2026-10-23 pooled behaviour.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    sys.path.insert(0, str(ROOT / "scripts"))
    import importlib
    return importlib.import_module(name)


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _trade(edge, pnl, days_ago, ts=None):
    """A closed paper record as the openers write it (NET-priced via paper_net)."""
    return {"edge": edge, "symbol": "BTC/USDT", "pnl": pnl,
            "cost": 0.0015536, "pnl_net": pnl,
            "ts_utc": ts if ts is not None else _ago(days_ago)}


def _edge_row(name, n, net, wr=45.0, win_usd=None, loss_usd=None):
    w = int(round(n * wr / 100.0))
    if win_usd is None or loss_usd is None:
        win_usd = max(net, 0) + 3.0 * max(n - w, 1) * 0.6
        loss_usd = win_usd - net
    return {"edge": name, "n": n, "win_rate": wr, "win_rate_gross": wr,
            "avg_pnl": net / n, "net_pnl": net, "gross_pnl": net,
            "gross_win": win_usd, "gross_loss": loss_usd,
            "symbols": ["BTC/USDT"]}


def _paper():
    return {"n": 100, "win_rate": 42.0, "win_rate_gross": 47.0, "ev": -0.001,
            "ev_gross": 0.004, "profit_factor": 0.95, "total_pnl": -1.0,
            "gross_pnl": 3.0, "uncosted": 0}


def _live():
    return {"n": 20, "win_rate": 42.0, "win_rate_gross": 42.0, "ev": -0.001,
            "ev_gross": -0.001, "profit_factor": 0.8, "total_pnl": -0.2,
            "gross_pnl": -0.2, "uncosted": 0}


# The measured shape: a strong older month, a losing recent one. Each trade is
# a real-size paper fill (~0.05 win / ~0.05 loss) so the PF lands near the book.
def _aging_book():
    out = []
    # 2026-09-like: 60 wins of +0.12 and 20 losses of -0.10 over 60-90d ago
    for _ in range(60):
        out.append(_trade("loose", +0.12, 70))
    for _ in range(20):
        out.append(_trade("loose", -0.10, 65))
    # 2026-10-like: 30 wins of +0.08 and 50 losses of -0.14 inside the window
    for _ in range(30):
        out.append(_trade("validated", +0.08, 10))
    for _ in range(50):
        out.append(_trade("validated", -0.14, 5))
    return out


_FILL = {"loose": _ago(1), "validated": _ago(0.5)}


def test_window_uses_live_edges_only_and_respects_the_cutoff():
    il = _load("improvement_loop")
    trades = _aging_book() + [
        _trade("supertrend", -5.0, 5),      # dead edge inside window: excluded
        _trade("loose", -9.0, 200),         # live edge outside window: excluded
    ]
    meta = {"last_fill": {**_FILL, "supertrend": _ago(45)}}
    r = il._recent_live_pf(trades, meta)
    assert r["available"] is True
    assert r["days"] == il.PF_WINDOW_DAYS
    # 60 wins outside + 30 wins inside; 20 losses outside + 50 losses inside
    assert r["n"] == 80, "only live-edge trades inside the window may count"
    assert r["net"] == pytest.approx(30 * 0.08 - 50 * 0.14, abs=1e-6)


def test_a_book_that_pays_over_its_life_but_loses_now_draws_advice():
    """THE DEFECT: lifetime PF 1.03 cleared the 1.02 floor and said nothing.

    The pooled figure below is ~1.00 (just under the floor in this smaller
    fixture, but the point is the WINDOW at 0.75 must be what speaks). The
    real book read 1.03 pooled — see the module docstring.
    """
    il = _load("improvement_loop")
    trades = _aging_book()
    meta = {"last_fill": _FILL, "redundant": {}}
    meta["recent_pf"] = il._recent_live_pf(trades, meta)
    assert meta["recent_pf"]["pf"] < il.PF_NET_FLOOR
    # pooled row constructed from the same dollars: +7.2 +2.4 wins / -2.0 -7.0
    rows = [_edge_row("loose", 80, +5.20, 75.0, win_usd=60 * 0.12, loss_usd=20 * 0.10),
            _edge_row("validated", 80, -4.60, 37.5, win_usd=30 * 0.08, loss_usd=50 * 0.14)]
    split = il._split_live_pf(rows, meta)
    assert split["live"]["pf"] > meta["recent_pf"]["pf"], (
        "fixture must have a STRONGER pooled figure than windowed one, or it "
        "cannot demonstrate the masking defect")
    out = il._suggest(_paper(), _live(), [], rows, meta)
    rr = [s["idea"] for s in out if s["type"] == "rr_tune"]
    assert rr, "a book losing inside its own window must draw exit advice"
    assert f"LAST {il.PF_WINDOW_DAYS}d" in rr[0]


def test_the_windowed_advice_states_both_figures_and_the_window():
    """Rule #12: the verdict must say what it was measured on, and on what
    cohort. Quoting only the pooled number is what hid the loss for 4 weeks."""
    il = _load("improvement_loop")
    trades = _aging_book()
    meta = {"last_fill": _FILL, "redundant": {}}
    meta["recent_pf"] = il._recent_live_pf(trades, meta)
    events = [_edge_row("loose", 80, +5.20, 75.0),
              _edge_row("validated", 80, -4.60, 37.5)]
    out = il._suggest(_paper(), _live(), [], events, meta)
    idea = [s["idea"] for s in out if s["type"] == "rr_tune"][0]
    assert "whole life" in idea, "the pooled figure must be shown beside the window"
    assert f"{il.PF_WINDOW_DAYS}d" in idea, "the window length must be stated"
    assert "loose" in idea and "validated" in idea, "the cohort must be named"
    assert "1.02" in idea, "the bar it was judged against must be stated"


def test_a_book_paying_inside_the_window_draws_no_exit_advice():
    """The window is a lens, not a mute button — and not an alarm either."""
    il = _load("improvement_loop")
    good = ([_trade("loose", +0.20, 10) for _ in range(40)]
            + [_trade("loose", -0.08, 8) for _ in range(15)])
    meta = {"last_fill": {"loose": _ago(0.5)}, "redundant": {}}
    meta["recent_pf"] = il._recent_live_pf(good, meta)
    assert meta["recent_pf"]["pf"] > il.PF_NET_FLOOR
    out = il._suggest(_paper(), _live(), [],
                      [_edge_row("loose", 55, +6.8, 72.0)], meta)
    assert not [s for s in out if s["type"] == "rr_tune"]


def test_a_thin_window_cannot_manufacture_a_verdict():
    il = _load("improvement_loop")
    thin = [_trade("loose", -5.0, 3)] * 5      # 5 trades, all losers, tiny n
    meta = {"last_fill": {"loose": _ago(0.5)}, "redundant": {}}
    meta["recent_pf"] = il._recent_live_pf(thin, meta)
    assert meta["recent_pf"]["n"] == 5
    out = il._suggest(_paper(), _live(), [],
                      [_edge_row("loose", 500, +50.0, 60.0)], meta)
    rr = [s["idea"] for s in out if s["type"] == "rr_tune"]
    assert not rr, "a 5-trade window must not carry a PF verdict"


def test_an_undated_record_cannot_vote_on_a_window():
    """A record that cannot be placed in time must not be silently dated 'now'."""
    il = _load("improvement_loop")
    trades = [_trade("loose", -1.0, 5, ts=""),
              _trade("loose", -1.0, 5, ts="not-a-date"),
              _trade("loose", +0.5, 5)]
    r = il._recent_live_pf(trades, {"last_fill": {"loose": _ago(0.5)}})
    assert r["n"] == 1, "only the parseable record may count"
    assert r["undated"] == 2, "the excluded records must be counted, not dropped"


def test_no_liveness_data_degrades_to_the_pooled_rule():
    """A broken liveness reader must not silence rule 3 (pre-fix behaviour).

    `last_fill` present but EMPTY is the real degraded state (`edge_liveness`
    returned nothing); a dict with no `last_fill` key is a bare edge->fill map
    and is a different path — see _split_live_pf's docstring.
    """
    il = _load("improvement_loop")
    r = il._recent_live_pf(_aging_book(), {})
    assert r["available"] is False
    out = il._suggest(_paper(), _live(), [],
                      [_edge_row("loose", 500, -4.0, 40.0)],
                      {"last_fill": {}, "redundant": {}})
    rr = [s["idea"] for s in out if s["type"] == "rr_tune"]
    assert len(rr) == 1
    assert "no liveness data" in rr[0]
    assert "LAST" not in rr[0], "no window may be claimed when none was computed"
