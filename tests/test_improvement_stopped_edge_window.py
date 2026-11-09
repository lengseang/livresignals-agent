"""A positive lifetime net beside the word STOPPED is an invitation to un-stop a loser.

Context (2026-11-08). The improvement report's per-edge history note named every
dead/stopped edge with its LIFETIME net only:

    "loose (n=1261, +3.37 net, last fill 2026-10-11) — STOPPED"

`loose` is the book's only net-POSITIVE cohort over its life, so that line reads
"the engine switched off our only winner". It did not. Measured the same day,
the 28 days up to the stop were n=17, -0.20 net — the edge was LOSING when
`self_improve` stood it down (2026-10-11, applied on an n=17 wr=29% sample), and
its +3.37 is two good September weeks (wk2 +7.28, wk3 +3.25) carried as a
lifetime mean across five losing weeks after them.

So the decision was right and the sentence was wrong — and the sentence is the
only thing the owner reads. A lifetime mean quoted next to STOPPED, with no
window, is the same defect the windowed-PF rule (2026-10-23, PF_WINDOW_DAYS)
fixed for the aggregate, left unfixed on the per-edge line.

Fix: every dead/stopped edge whose lifetime net is POSITIVE carries its recent
net in the same line, so the lifetime figure can never be read without the
number that justified the stop. A negative lifetime edge needs no window — it is
not going to tempt anyone.

What these tests pin:
  * a positive-lifetime dead edge carries its recent net;
  * a negative-lifetime dead edge does NOT grow the line (no noise);
  * the window is the SAME PF_WINDOW_DAYS the aggregate uses (one definition);
  * `_recent_edge_net` attributes by the one-book rule and windows by time;
  * an undateable trade never votes on a window;
  * a missing/empty recent map degrades to the old line (older callers);
  * the measured 2026-11-08 numbers reproduce the corrected sentence.
"""
from __future__ import annotations

import importlib.util
import re
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


def _edge_row(name, n=30, net=-1.0, wr=30.0):
    win, loss = (net * 1.8, net * 0.8) if net > 0 else (abs(net), abs(net) * 2)
    return {"edge": name, "n": n, "win_rate": wr, "win_rate_gross": wr,
            "avg_pnl": net / n, "net_pnl": net, "gross_pnl": net,
            "gross_win": win, "gross_loss": loss, "symbols": ["BTC/USDT"]}


def _paper(n=100, wr=42.0):
    return {"n": n, "win_rate": wr, "win_rate_gross": wr + 4, "ev": -0.001,
            "ev_gross": 0.004, "profit_factor": 0.95, "total_pnl": -1.0,
            "gross_pnl": 3.0, "uncosted": 0}


def _note(edges, last_fill, stopped=None, recent_edge_net=None):
    """Run `_suggest` and return the `edge_not_live` text ('' if none)."""
    il = _load("improvement_loop")
    live = {"n": 20, "win_rate": 42.0, "win_rate_gross": 42.0, "ev": -0.001,
            "ev_gross": -0.001, "profit_factor": 0.8, "total_pnl": -0.2,
            "gross_pnl": -0.2, "uncosted": 0}
    meta = {"stale": False, "last_fill": last_fill, "redundant": {},
            "stopped": stopped or {},
            "recent_pf": il._recent_live_pf([], {"last_fill": last_fill})}
    if recent_edge_net is not None:
        meta["recent_edge_net"] = recent_edge_net
    out = il._suggest(_paper(), live, [], edges, meta)
    for s in out:
        if s["type"] == "edge_not_live":
            return s["idea"]
    return ""


# ---------------------------------------------------------------- the core fix
def test_positive_lifetime_dead_edge_carries_its_recent_net():
    """The measured 2026-11-08 case: loose +3.37 life / -0.20 in 28d, STOPPED."""
    edges = [_edge_row("validated", n=50, net=-5.28, wr=44),
             _edge_row("loose", n=1261, net=3.37, wr=43)]
    fills = {"validated": _ago(0.1), "loose": _ago(28)}
    note = _note(edges, fills,
                 stopped={"loose": {"source": "signals-state.json"}},
                 recent_edge_net={"loose": {"n": 17, "net": -0.20, "days": 28}})
    assert "loose" in note
    assert "+3.37 net" in note, "the lifetime mean must stay — it is a fact"
    assert "last 28d: n=17, -0.20 net" in note, \
        "a positive lifetime mean MUST be reconciled with the window that stopped it"
    assert "STOPPED" in note


def test_negative_lifetime_dead_edge_does_not_grow_the_line():
    """supertrend is -1.89 life; nobody needs a window to believe it is bad."""
    edges = [_edge_row("validated", n=50, net=-1.0),
             _edge_row("supertrend", n=121, net=-1.89)]
    fills = {"validated": _ago(0.1), "supertrend": _ago(30)}
    note = _note(edges, fills,
                 stopped={"supertrend": {"source": "signals-state.json"}},
                 recent_edge_net={"supertrend": {"n": 0, "net": 0.0, "days": 28}})
    assert "supertrend" in note
    assert "last 28d" not in note.split("supertrend")[1].split(";")[0], \
        "a negative lifetime edge must not gain a window it does not need"


def test_the_window_is_the_aggregate_window_not_a_second_definition():
    il = _load("improvement_loop")
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    inside = {"edge": "loose", "ts_utc": (now - timedelta(days=il.PF_WINDOW_DAYS - 1)).isoformat()}
    outside = {"edge": "loose", "ts_utc": (now - timedelta(days=il.PF_WINDOW_DAYS + 1)).isoformat()}
    got = il._recent_edge_net([inside, outside], now=now)
    assert got["loose"]["n"] == 1, "the boundary must be PF_WINDOW_DAYS"
    assert got["loose"]["days"] == il.PF_WINDOW_DAYS


# ------------------------------------------------------------- _recent_edge_net
def test_attribution_follows_the_one_book_rule():
    """`edge` tag wins; else `loose` flag; else `validated` — same as the split."""
    il = _load("improvement_loop")
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    ts = (now - timedelta(days=1)).isoformat()
    trades = [
        {"edge": "loose", "ts_utc": ts, "pnl": 1.0, "size": 5.0},
        {"loose": True, "ts_utc": ts, "pnl": 1.0, "size": 5.0},
        {"ts_utc": ts, "pnl": 1.0, "size": 5.0},
        {"edge": "supertrend", "ts_utc": ts, "pnl": 1.0, "size": 5.0},
    ]
    got = il._recent_edge_net(trades, now=now)
    assert got["loose"]["n"] == 2, "an untagged `loose` flag belongs to loose"
    assert got["validated"]["n"] == 1, "a fully untagged fill belongs to validated"
    assert got["supertrend"]["n"] == 1


def test_undateable_trade_never_votes():
    il = _load("improvement_loop")
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    ts = (now - timedelta(days=1)).isoformat()
    got = il._recent_edge_net([
        {"edge": "loose", "ts_utc": ts, "pnl": 1.0, "size": 5.0},
        {"edge": "loose", "pnl": 99.0, "size": 5.0},          # no ts
        {"edge": "loose", "ts_utc": "not-a-date", "pnl": 99.0, "size": 5.0},
    ], now=now)
    assert got["loose"]["n"] == 1, "a record that cannot be dated must not vote"


def test_recent_net_is_costed_like_the_book():
    """A windowed net must use paper_net, not gross pnl — same basis as the card."""
    il = _load("improvement_loop")
    try:
        from paper_net import net_of
    except Exception:  # pragma: no cover — env without the sibling module
        return
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    t = {"edge": "loose", "ts_utc": (now - timedelta(days=1)).isoformat(),
         "pnl": 0.10, "size": 5.0}
    got = il._recent_edge_net([t], now=now)
    # `_recent_edge_net` rounds its sum to 4dp (the same precision every other
    # figure in the report carries), so compare at that precision, not to 1e-9.
    assert abs(got["loose"]["net"] - net_of(t)[0]) < 5e-5


# --------------------------------------------------------------- degradation
def test_missing_recent_map_degrades_to_the_old_line():
    edges = [_edge_row("validated", n=50, net=-1.0),
             _edge_row("loose", n=1261, net=3.37)]
    fills = {"validated": _ago(0.1), "loose": _ago(28)}
    note = _note(edges, fills, stopped={"loose": {"source": "signals-state.json"}},
                 recent_edge_net=None)
    assert "loose" in note and "STOPPED" in note
    assert "last 28d" not in note, "no recent map: keep the pre-fix wording"


def test_empty_recent_entry_degrades_to_the_old_line():
    edges = [_edge_row("validated", n=50, net=-1.0),
             _edge_row("loose", n=1261, net=3.37)]
    fills = {"validated": _ago(0.1), "loose": _ago(28)}
    note = _note(edges, fills, stopped={"loose": {"source": "signals-state.json"}},
                 recent_edge_net={"loose": {"n": 0, "net": 0.0, "days": 28}})
    assert "last 28d" not in note, "a zero-fill window must not print 'n=0'"


# ------------------------------------------------------------ wiring, not words
def test_analyze_computes_recent_edge_net():
    src = (SCRIPTS / "improvement_loop.py").read_text()
    assert re.search(r'edge_meta\["recent_edge_net"\]\s*=\s*_recent_edge_net\(', src), \
        "_recent_edge_net is worthless if analyze() never attaches it"
