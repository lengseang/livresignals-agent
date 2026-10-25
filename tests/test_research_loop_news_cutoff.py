"""research_loop must announce a NEW BEST only when the best actually moved (2026-10-24).

THE DEFECT THIS PINS
--------------------
`_change_lines(prev, cur, best, live_row)` builds the owner-facing lines for the
nightly research loop. Its first branch was

    if score(best_row) > score(pb):     # pb = best of the PREVIOUS run

which is already the correct cutoff, and the branch below it calls
`lead_is_coin_fragile(best_row, live_row)` and may emit "LIVE EDGE DECAYED".

On 2026-10-24 the live cache made the best config the CURRENT WINNER (sma_r=100,
33.52%, 4/4) while the LIVE config (sma_r=150, 27.08%, 4/4) sat ABOVE its own
leave-one-coin-out ceiling — a regime-break line, correctly emitted. The same
cache also produced a REFUSED-NEW-BEST line naming sma_r=100 and describing it
as "the lead over LIVE", so one message said of sma_r=100 both "not an edge; the
incumbent stands" and implied it WAS the incumbent the live config had decayed
from. Two contradictory claims about the same config in the same Telegram
message, and the reader cannot tell which config the loop actually runs.

Why the branch fired is the interesting part: the noise check (a) tests whether
the challenger's lead sits inside the LIVE band. When the challenger is itself
the incumbent, `lead = best - live = 0`, and 0 is inside any band — so the check
can only ever report "fragile" for the live config compared with itself. The
code is not wrong about the numbers; it is asking a question that has no
meaning for that pair.

WHAT THE FIX IS
---------------
Keep the cutoff (a config that has not out-scored the previous best is not
news), and make it explicit that a TIE is not news either: `>` stays, and the
reason is documented, because a tie previously admitted a refusal whose reason
was computed against a stale challenger. The decay branch is unchanged — it
answers a different, still-meaningful question.

Hermetic: no cache, no live state, no network. The `prev` here is a dict.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

import research_loop as rl  # noqa: E402


def _row(sma_r, median, pos=4, frag=None):
    return {"cfg": {"topn": 3, "hold": 21, "risk": 0.01, "sma_r": sma_r},
            "median": median, "pos_folds": pos, "n_folds": 4,
            "trades": 700, "rets": [median] * 4,
            "fragility": frag or {"tested": False}}


def _band(lo, hi):
    return {"tested": True, "draws": 12, "loo_min": lo, "loo_max": hi,
            "loo_drop_max": 5.0, "per_coin": {"DOGE": {"ret": lo}}}


def _cur(best, live):
    return {"ts": "2026-10-24T01:30:00+00:00",
            "rows": [best, live], "bench_btc_median": -22.0}


def test_no_new_best_line_when_the_best_config_has_not_moved():
    best = _row(100, 33.52, frag=_band(9.71, 19.02))
    live = _row(150, 27.08, frag=_band(5.70, 23.48))
    lines = rl._change_lines(prev={"rows": [best, live]}, cur=_cur(best, live),
                             best=best, live_row=live)
    assert not any("NEW BEST" in c for c in lines), lines


def test_no_new_best_line_on_a_tie():
    best = _row(100, 33.52)
    live = _row(150, 27.08)
    prior = _row(200, 33.52)          # same score, different config
    lines = rl._change_lines(prev={"rows": [prior, live]}, cur=_cur(best, live),
                             best=best, live_row=live)
    assert not any("NEW BEST" in c for c in lines), lines


def test_no_decay_or_new_best_line_when_live_stays_inside_its_band():
    """The fix must not silence the branch that IS meaningful — and must not
    invent one. LIVE at its ceiling is inside its noise, so the loop says
    nothing about decay (that direction is pinned by
    `test_decay_line_fires_when_live_breaks_its_ceiling` below).
    """
    best = _row(100, 33.52, frag=_band(9.71, 19.02))
    live = _row(150, 27.08, frag=_band(5.70, 27.08))   # 27.08 == ceiling: inside
    assert rl.lead_is_coin_fragile(best, live)["live_fragile"] is False
    lines = rl._change_lines(prev={"rows": [best]}, cur=_cur(best, live),
                             best=best, live_row=live)
    assert not any("DECAYED" in c for c in lines), lines


def test_decay_line_fires_when_live_breaks_its_ceiling():
    best = _row(100, 33.52, frag=_band(9.71, 19.02))
    live = _row(150, 27.08, frag=_band(5.70, 23.48))   # 27.08 > ceiling 23.48
    lines = rl._change_lines(prev={"rows": [best]}, cur=_cur(best, live),
                             best=best, live_row=live)
    decay = [c for c in lines if "DECAYED" in c]
    assert len(decay) == 1, lines
    assert "ceiling" in decay[0] and "floor" not in decay[0], decay[0]
    assert "sma_r': 150" in decay[0], "the line must name the config it is about"


def test_genuinely_new_best_is_still_announced():
    old = _row(150, 10.0)
    live = _row(150, 10.0)
    new = _row(100, 33.52, frag=_band(20.0, 30.0))
    lines = rl._change_lines(prev={"rows": [old, live]}, cur=_cur(new, live),
                             best=new, live_row=live)
    assert any(c.startswith("NEW BEST") or c.startswith("REFUSED 'NEW BEST'")
               for c in lines), lines
