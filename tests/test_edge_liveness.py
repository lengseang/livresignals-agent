"""A per-edge verdict must be about an edge that is still TRADING.

Context (2026-10-21): `improvement_loop` computes per-edge stats over the WHOLE
closed book and quotes an unbounded historical mean. On that date the report
carried seven "Edge '<x>' DECAYING (net) ... tighten its entry or stand it
down" verdicts — for supertrend, bollinger, ema-cross, momentum, pullback,
breakout and rsi2. Every one of those edges had been DISABLED in
data/signals-state.json AND had produced no fill since mid-September:

    pullback     last fill 2026-09-06   (46d)
    rsi2         last fill 2026-09-08   (43d)
    supertrend   last fill 2026-09-08   (43d)
    bollinger    last fill 2026-09-10   (41d)
    ema-cross    last fill 2026-09-12   (39d)
    momentum     last fill 2026-09-13   (38d)
    breakout     last fill 2026-09-13   (38d)

"Stand it down" for an edge that cannot open is a no-op dressed as an action,
and "tighten its entry" is advice about a design nobody can change through the
book. The reader could not tell those verdicts from live ones.

The mirror defect: a frozen record with a GOOD mean reads as proven forever, so
the same guard has to stop promotion of an edge that stopped filling.

What these tests pin, all through the real decision functions:
  * a stale edge draws NO tighten/stand-down verdict,
  * it does not silently vanish either — one note names it and its last fill,
  * a live edge still draws its verdict (the guard is not a mute button),
  * an edge whose last fill cannot be dated is treated as LIVE on the read
    side (precision over silence) but NOT promotable on the write side,
  * a redundant copy is not given an independent verdict.
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


def _edge_row(name, n=30, net=-1.0, wr=30.0, avg=None):
    return {"edge": name, "n": n, "win_rate": wr, "win_rate_gross": wr,
            "avg_pnl": avg if avg is not None else net / n,
            "net_pnl": net, "gross_pnl": net, "symbols": ["BTC/USDT"]}


def _paper(n=100, wr=42.0):
    return {"n": n, "win_rate": wr, "win_rate_gross": wr + 4, "ev": -0.001,
            "ev_gross": 0.004, "profit_factor": 0.95, "total_pnl": -1.0,
            "gross_pnl": 3.0, "uncosted": 0}


def _suggest(edges, last_fill, redundant=None):
    il = _load("improvement_loop")
    live = {"n": 20, "win_rate": 42.0, "win_rate_gross": 42.0, "ev": -0.001,
            "ev_gross": -0.001, "profit_factor": 0.8, "total_pnl": -0.2,
            "gross_pnl": -0.2, "uncosted": 0}
    meta = {"stale": False, "last_fill": last_fill,
            "redundant": redundant or {}}
    return il._suggest(_paper(), live, [], edges, meta)


def test_stale_edge_draws_no_live_tighten_or_stand_down_verdict():
    """The measured defect: supertrend -1.87 net, silent 43 days."""
    out = _suggest([_edge_row("supertrend", n=134, net=-1.87, wr=31.3),
                    _edge_row("validated", n=900, net=+1.3, wr=45.0)],
                   {"supertrend": _ago(43), "validated": _ago(0.1)})
    decay = [s["idea"] for s in out if s["type"] == "edge_decay"]
    assert not any("supertrend" in d for d in decay), \
        "a design that has not filled in 43 days must not be told to tighten"
    assert not any("stand it down" in d and "supertrend" in d for d in decay)


def test_stale_edge_is_not_silently_dropped():
    """Silence must be explained: one note names the edge and its last fill."""
    out = _suggest([_edge_row("supertrend", n=134, net=-1.87, wr=31.3)],
                   {"supertrend": _ago(43)})
    notes = [s for s in out if s["type"] == "edge_not_live"]
    assert len(notes) == 1
    idea = notes[0]["idea"]
    assert "supertrend" in idea and "n=134" in idea and "-1.87" in idea
    assert _ago(43)[:10] in idea, "the note must state WHEN it last filled"
    assert "HISTORY" in idea, "the note must say the verdict is history"


def test_live_edge_still_draws_its_verdict():
    """The guard is a time filter, not a mute button."""
    out = _suggest([_edge_row("loose", n=1376, net=-2.0, wr=40.0)],
                   {"loose": _ago(0.2)})
    decay = [s["idea"] for s in out if s["type"] == "edge_decay"]
    assert any("loose" in d and "stand it down" in d for d in decay)
    assert not [s for s in out if s["type"] == "edge_not_live"]


def test_boundary_is_days_not_number_of_trades():
    """An edge idle 20d with n=1000 is stale; one idle 2d with n=12 is live."""
    ed = [_edge_row("big", n=1000, net=-5.0), _edge_row("small", n=12)]
    out = _suggest(ed, {"big": _ago(20), "small": _ago(2)})
    decay = [s["idea"] for s in out if s["type"] == "edge_decay"]
    assert any("small" in d for d in decay)
    assert not any("big" in d for d in decay), \
        "a large n is not evidence of liveness"


def test_undateable_edge_is_treated_as_live_and_still_judged():
    """No liveness data (reader failed / odd stamp) degrades to old behaviour."""
    out = _suggest([_edge_row("mystery", n=50, net=-3.0)],
                   {"mystery": "not-a-timestamp"})
    assert any("mystery" in s["idea"] for s in out if s["type"] == "edge_decay")
    out2 = _suggest([_edge_row("mystery", n=50, net=-3.0)], {})
    assert any("mystery" in s["idea"] for s in out2 if s["type"] == "edge_decay")


def test_redundant_copy_gets_no_independent_verdict():
    out = _suggest([_edge_row("breakout", n=18, net=-0.44),
                    _edge_row("loose", n=1376, net=-2.0, wr=41.0)],
                   {"breakout": _ago(0.5), "loose": _ago(0.5)},
                   redundant={"breakout": "loose"})
    decay = [s["idea"] for s in out if s["type"] == "edge_decay"]
    assert not any("breakout" in d for d in decay), \
        "a copy's evidence is its parent's; do not judge it as a second edge"
    assert any(s["type"] == "edge_redundant" and "breakout ~ loose" in s["idea"]
               for s in out)


# ---------------------------------------------------------------- write side
def test_promotion_refuses_a_frozen_record():
    """"Proven" must mean proven WHILE trading (self_improve)."""
    si = _load("self_improve")
    assert si._filled_since(_ago(1), 14) is True
    assert si._filled_since(_ago(40), 14) is False
    # a stamp we cannot date is NOT promotable — the write side fails closed
    assert si._filled_since("", 14) is False
    assert si._filled_since(None, 14) is False


def test_promotion_gate_is_wired_into_main():
    """Pin the wiring, not a source string: the promote branch must consult
    liveness and redundancy before appending to `promoted`."""
    src = (SCRIPTS / "self_improve.py").read_text(encoding="utf-8")
    promote = src.split('elif st["n"] >= 10 and st["wr"] >= 60', 1)
    assert len(promote) == 2, "the promote branch must still exist"
    branch = promote[1].split('elif st["n"] >= 10', 1)[0]
    assert "_filled_since(lf, LIVE_DAYS)" in branch, \
        "promotion must be refused on a frozen (not-live) edge"
    assert "redundant[e]" in branch, \
        "promotion must be refused on a redundant copy"
