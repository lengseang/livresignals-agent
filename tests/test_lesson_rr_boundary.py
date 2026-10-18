"""An RR of exactly 2.0 is a DESIGNED 2:1 — it must never be judged as "below 2.0".

Found 2026-10-18 on the real lesson ledger (`data/trade-lessons.jsonl`, 3162
lessons). The 2:1 geometry is the most common single design on this system:
`abs(tp-entry)/abs(entry-sl)` with a stop set at 1x and a target at 2x. Binary
floats make that ratio land a hair either side of the literal:

    entry=1.3607 tp=1.3879 sl=1.3471 -> 1.9999999999999836
    entry=2421.71 tp=2470.1442 sl=2397.4929 -> 2.0000000000000187

`classify()` then compared the raw float to `RR_GOOD = 2.0`, so the SAME 2:1
setup — differing only in which way the division rounded — got four different
verdicts:

    119  GOOD DECISION, POOR OUTCOME  "RR 2.00 is inside the winning population"
     56  AS DESIGNED                  "RR 2.00 below 2.0; this population loses"
     28  ALIGNED
     21  GOOD OUTCOME, POOR DECISION  "RR 2.00 is inside the losing population"

Three of those sentences print the identical string "RR 2.00" and contradict
each other. The 56 `AS DESIGNED` rows are the damaging ones: a trade designed at
the threshold is labelled a member of "the measured population that loses", and
`improve()` then tells the owner "skipping it would have avoided this trade
entirely" — a stand-down recommendation for the exact geometry the owner chose.

Two independent defects, both pinned below:

  1. BOUNDARY. The comparison must be tolerance-aware. An RR within float noise
     of the threshold IS the threshold, and the verdict must say "at" not
     "below".
  2. SENTENCE/VERDICT UNITY. A verdict whose text says "below 2.0" must be
     produced only by a trade that is below 2.0. The note currently prints
     `rr:.2f`, which ROUNDS 1.996 up to "2.00" — so a genuinely sub-threshold
     trade also emits the self-contradicting line "RR 2.00 below 2.0".

The band matters: of the ~284 lessons in 1.9995-2.0005, the cohort is net
-10.53 over 25% WR, so this is not a cosmetic label on a profitable group — it
is the largest single bucket in the book and it is being told two opposite
stories about itself.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path("/opt/trading-agent")
LOOP = Path("/home/ubuntu/hermes-home/scripts/trade_lesson_loop.py")


def _load_loop():
    """Load the lesson loop by path.

    The loop lives outside the repo (it is the agent's own cron script), so it
    is not importable as `scripts.x`. Guard with a skip so a checkout without
    the agent home still runs the rest of the suite.
    """
    spec = importlib.util.spec_from_file_location("trade_lesson_loop", LOOP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["trade_lesson_loop"] = mod
    spec.loader.exec_module(mod)
    return mod


import pytest

pytestmark = pytest.mark.skipif(
    not LOOP.exists(), reason="agent-home lesson loop not present in this checkout")

VALID_POP = {"instrument_invalid": False, "instrument_evidence": {}}


def _trade(entry, tp, sl, pnl):
    return {"symbol": "XRP/USDT", "ts_utc": "2026-10-18T00:00:00+00:00",
            "entry": entry, "tp": tp, "sl": sl, "pnl": pnl, "reason": "TIME"}


def test_float_below_2_is_judged_at_the_threshold_not_below_it():
    """The real regressing record: a designed 2:1 that floats to 1.9999999999999836."""
    loop = _load_loop()
    t = _trade(1.3607, 1.3879, 1.3471, -0.02)
    assert loop._rr(t) < 2.0, "precondition: this record does float below 2.0"
    verdict, note = loop.classify(t, VALID_POP)
    assert verdict != "AS DESIGNED", (
        f"a 2:1 design was judged {'AS DESIGNED'} with note {note!r}")
    assert "below" not in note, f"note still claims 'below': {note!r}"
    assert verdict in ("GOOD DECISION, POOR OUTCOME", "ALIGNED")


def test_float_above_2_is_judged_at_the_threshold_too():
    """The mirror case: the same 2:1 floating to 2.0000000000000187."""
    loop = _load_loop()
    t = _trade(2421.71, 2470.1442, 2397.4929, -0.02)
    assert loop._rr(t) > 2.0, "precondition: this record does float above 2.0"
    verdict, note = loop.classify(t, VALID_POP)
    assert verdict != "GOOD OUTCOME, POOR DECISION", note
    assert verdict in ("GOOD DECISION, POOR OUTCOME", "ALIGNED")


def test_a_genuinely_sub_threshold_rr_never_prints_rounds_up_to_2_00():
    """A real 1.996 must not render as 'RR 2.00 below 2.0' — the printed number
    has to agree with the verdict it sits in."""
    loop = _load_loop()
    # stop 1.0 wide, target 1.996 wide -> rr = 1.996
    t = _trade(100.0, 101.996, 99.0, -0.02)
    rr = loop._rr(t)
    assert 1.99 < rr < 2.0
    verdict, note = loop.classify(t, VALID_POP)
    assert verdict == "AS DESIGNED", verdict
    assert "RR 2.00" not in note, f"note rounds up into self-contradiction: {note!r}"
    assert "1.996" in note or "1.99" in note, note


def test_improve_does_not_tell_the_owner_to_skip_a_2_to_1_setup():
    """`improve()` derived its advice from the same raw comparison, so a 2:1
    design was told 'skipping it would have avoided this trade entirely'."""
    loop = _load_loop()
    t = _trade(1.3607, 1.3879, 1.3471, -0.02)
    verdict, _ = loop.classify(t, VALID_POP)
    advice = loop.improve(t, verdict, VALID_POP)
    joined = " | ".join(advice)
    assert "skipping it would have avoided" not in joined, joined


def test_low_rr_is_still_flagged_but_no_longer_told_to_skip_itself():
    """A 1.5 setup must still be NOTICED — but the skip advice is gone.

    UPDATED 2026-10-18. The original asserted "skipping it would have avoided
    this trade", on the premise that RR<2 is "the losing population". That
    premise was falsified: `sl` is overwritten by the trail, and on DESIGNED
    geometry the RR<2 population is net-positive overall (+9.95 first half /
    -1.20 second). Telling the owner to skip it would delete the bigger cohort.
    The guard that still matters: a low-RR setup is named as low-RR, and the
    advice neither skips it nor promotes it.
    """
    loop = _load_loop()
    t = _trade(100.0, 101.5, 99.0, -0.05)
    verdict, note = loop.classify(t, VALID_POP)
    assert verdict == "AS DESIGNED", note
    advice = " | ".join(loop.improve(t, verdict, VALID_POP))
    assert "below RR" in advice, advice
    assert "skipping it would have avoided" not in advice, (
        f"advice still orders a skip on a finding that was retracted: {advice}")
    assert "not a reason to skip" in advice, advice


def test_rr_at_threshold_matches_the_population_split_used_elsewhere():
    """The threshold is used in three places (classify, improve, why). All three
    must agree on a boundary trade — one comparison helper, not three copies."""
    loop = _load_loop()
    t = _trade(1.3607, 1.3879, 1.3471, -0.02)
    assert loop._rr_meets_good(loop._rr(t)) is True
    assert loop._rr_meets_good(1.5) is False
    assert loop._rr_meets_good(2.0) is True
    why = " | ".join(loop.why(t, VALID_POP))
    assert "below the 2.0 threshold" not in why, why


def test_threshold_validity_flags_a_threshold_the_population_does_not_respect():
    """The corrected boundary exposes a second, bigger question: RR_GOOD=2.0 was
    derived when 'RR>=2 carried all the net' was true of the book. It no longer
    is (2.0-2.1 band is net -11.59 over 240 trades). The rollup must SAY so
    rather than keep quoting a boundary the population ignores."""
    loop = _load_loop()
    lessons = (
        # >= 2.0 side: net positive overall, but only because of the high band
        [{"rr": 2.5, "pnl": 0.10, "verdict": "ALIGNED"} for _ in range(30)]
        + [{"rr": 2.0, "pnl": -0.05, "verdict": "AS DESIGNED"} for _ in range(30)]
        # < 2.0 side: clearly losing
        + [{"rr": 1.5, "pnl": -0.05, "verdict": "AS DESIGNED"} for _ in range(30)]
    )
    tv = loop.threshold_validity(lessons)
    assert tv["threshold"] == 2.0
    assert tv["good_side"]["net"] > 0 and tv["bad_side"]["net"] < 0
    # overall the signs differ, so ok is True -- but the FIRST paying band is 2.0
    # only if 2.0-2.05 pays. Here it does not, so it must report 2.5.
    assert tv["first_paying_band"] == 2.5, tv


def test_threshold_validity_is_quiet_when_the_boundary_is_honest():
    loop = _load_loop()
    lessons = ([{"rr": 2.5, "pnl": 0.10, "verdict": "ALIGNED"} for _ in range(30)]
               + [{"rr": 2.0, "pnl": 0.10, "verdict": "ALIGNED"} for _ in range(30)]
               + [{"rr": 1.5, "pnl": -0.05, "verdict": "AS DESIGNED"} for _ in range(30)])
    tv = loop.threshold_validity(lessons)
    assert tv["first_paying_band"] == 2.0
    assert tv["ok"] is True
    assert "note" not in tv


def test_threshold_validity_survives_an_empty_ledger():
    loop = _load_loop()
    tv = loop.threshold_validity([])
    assert tv["n"] == 0 and tv["ok"] is None
