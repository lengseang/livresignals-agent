# -*- coding: utf-8 -*-
"""The research loop's coin-fragility gate compared two different universes.

Found 2026-11-09 (overnight autonomous pass). Read-only: the real cache is
loaded through importlib, nothing here writes `data/`, `research/` or a book.

The claim pinned below, measured on the live cache via `rl.run()`:

  `lead_is_coin_fragile()` asked whether the RAW head-to-head lead — each
  config scored on the FULL universe — sat inside the LIVE config's
  leave-one-coin-out band. The challenger's leg was full-universe; the band
  was not. On 2026-11-09 (sma_r=100 vs LIVE 150) the raw lead was -3.63 and
  the live band [10.68, 44.55], so "inside" was False and the gate passed a
  challenger that LOSES under 15 of 23 single-coin drops. Paired on the same
  universe the lead is -13.17..+7.12 (median -1.86): noise.

  A band only says something about a lead when BOTH legs are measured on the
  same universe. `per_coin` already holds both legs, so the paired lead is a
  lookup — no extra walk of the harness.

  Two adjacent defects found by the same probe, both about an ADMISSION:
   - a SEEDED-SAMPLE band (LOO_DRAWS set) could admit a crown it is not
     entitled to; a sample may refuse, never admit;
   - `_seg_lead_frac` counts segments the challenger WINS, so a challenger
     with a NEGATIVE median lead can clear the 0.75 floor (measured: 0.88 at
     lead -18.73pp) and be treated as surviving that test.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CORE = ROOT / "research" / "06-walkforward.py"
CACHE = ROOT / "research" / "price-cache-1000d.json"

pytestmark = pytest.mark.skipif(
    not (CORE.exists() and CACHE.exists()),
    reason="research harness/cache absent (repo-only checkout)")

LIVE = {"topn": 3, "hold": 21, "risk": 0.01, "sma_r": 150}


def _rl():
    spec = importlib.util.spec_from_file_location(
        "rl_paired", ROOT / "scripts" / "research_loop.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["rl_paired"] = m
    spec.loader.exec_module(m)
    return m


def _row(sma_r, median, pos=4, loo=(10.0, 44.0), per_coin=None, **extra):
    frag = {"tested": True, "draws": len(per_coin or {}) or 23,
            "median": median, "loo_min": loo[0], "loo_max": loo[1],
            "loo_drop_mean": 0.0, "loo_drop_max": 0.0,
            "per_coin": per_coin or {},
            "basis": "exhaustive leave-one-coin-out over 23 coins"}
    frag.update(extra.pop("fragility_extra", {}))
    # halves: every leg earns the SAME little, so `_seg_lead_frac` is 0.0 and
    # every unit test below exercises ONE trigger at a time (the pattern the
    # 2026-10-19 suite uses). A test that wants the segment test to fire sets
    # `halves` on both rows explicitly.
    h = [median] * 8
    r = {"cfg": {"topn": 3, "hold": 21, "risk": 0.01, "sma_r": sma_r},
         "median": median, "pos_folds": pos, "n_folds": 4,
         "trades": 700, "rets": [median] * 4, "halves": h,
         "fragility": frag}
    r.update(extra)
    return r


# ------------------------------------------------- same-universe paired lead
def test_a_lead_inside_its_own_paired_band_is_refused():
    """The 2026-11-09 shape: raw lead OUTSIDE the live band, paired lead
    straddling zero. The old rule admitted it; this one refuses."""
    rl = _rl()
    live = _row(150, 36.0, loo=(10.68, 44.55))
    chal = _row(100, 32.4, loo=(14.62, 31.38))
    chal["fragility"]["lead_interval"] = (-13.17, 7.12)
    v = rl.lead_is_coin_fragile(chal, live)
    assert v["lead"] == -3.6
    assert v["inside_live_band"] is True
    assert v["inside_basis"] == "paired_leave_one_coin_out"
    assert v["fragile"] is True
    assert "paired lead" in v["reason"]
    assert "drops" in v["reason"] or "flips" in v["reason"]


def test_a_paired_lead_that_straddles_zero_refuses_even_when_the_raw_lead_is_just_outside():
    rl = _rl()
    live = _row(150, 30.0, loo=(5.0, 9.0))          # narrow band, lead 1.5 is OUTSIDE
    chal = _row(200, 31.5, loo=(28.0, 34.0))
    chal["fragility"]["lead_interval"] = (-2.0, 5.0)  # but the paired band straddles 0
    v = rl.lead_is_coin_fragile(chal, live)
    assert v["inside_live_band"] is True
    assert v["fragile"] is True


def test_a_paired_lead_clear_of_zero_survives_the_first_test():
    rl = _rl()
    live = _row(150, 10.0, loo=(8.0, 12.0))
    live["halves"] = [0, 0, 0, 0, 0, 0, 0, 0]
    chal = _row(200, 26.0, loo=(24.0, 28.0))
    chal["halves"] = [5, 5, 5, 5, 5, 5, 5, 5]
    chal["fragility"]["lead_interval"] = (14.0, 17.5)
    chal["fragility"]["loo_drop_max"] = 1.0
    v = rl.lead_is_coin_fragile(chal, live)
    assert v["inside_live_band"] is False
    assert v["fragile"] is False, v["reason"]


def test_a_paired_band_that_does_not_contain_the_published_lead_does_not_vote():
    """A band from a different pair must not decide this one.

    The path that produces this on the real grid is `run()` walking a
    SINGLETON challenger (only its own per_coin) and then pairing it with
    LIVE: the two rows walked different universes, so their drop labels do
    not line up. `run()` now withholds such a band upstream
    (`lead_interval_untested`); this pins the second lock — the gate itself
    refuses to let a band that is not about THIS lead decide it, even when a
    caller hands it one directly.
    """
    rl = _rl()
    live = _row(150, 30.0, loo=(5.0, 9.0))
    chal = _row(200, 31.0, loo=(28.0, 34.0))
    chal["fragility"]["lead_interval"] = (-5.0, 0.0)   # crafted: does not hold +1.0
    v = rl.lead_is_coin_fragile(chal, live)
    # falls back to "not inside" only because the band cannot be about this lead
    assert v["inside_live_band"] is False
    assert v["paired_lead_band"] == (-5.0, 0.0)


def test_rows_without_a_paired_band_keep_the_documented_fallback():
    rl = _rl()
    live = _row(150, 10.0, loo=(8.0, 12.0))
    chal = _row(200, 8.0, loo=(6.0, 7.0))
    chal["fragility"]["loo_drop_max"] = 0.0
    v = rl.lead_is_coin_fragile(chal, live)
    assert v["inside_basis"] == "live_band"
    assert v["paired_untested"] is True
    # raw lead -2.0 is BELOW [8, 12] -> not "inside" -> the old test could not
    # refuse it on its own; that is exactly why the paired band matters.
    assert v["inside_live_band"] is False
    # (d) fires precisely BECAUSE the raw test had no refusal: the challenger's
    # segments win small while its median lead is negative.
    assert "wins small" in v["reason"]


# ------------------------------------------------- a sample may not admit
def test_a_sampled_paired_band_may_refuse():
    rl = _rl()
    live = _row(150, 30.0, loo=(5.0, 9.0),
                fragility_extra={"sampled": True})
    chal = _row(200, 31.0, loo=(28.0, 34.0))
    chal["fragility"]["lead_vs_live"] = [-2.0, 0.5, 3.0]
    chal["fragility"]["paired_sampled"] = True
    v = rl.lead_is_coin_fragile(chal, live)
    assert v["inside_live_band"] is True
    assert v["fragile"] is True


def test_a_sampled_paired_band_cannot_admit_a_crown():
    rl = _rl()
    live = _row(150, 30.0, loo=(10.0, 40.0),
                fragility_extra={"sampled": True})
    chal = _row(200, 33.0, loo=(30.0, 36.0))
    chal["fragility"]["lead_vs_live"] = [1.0, 3.0, 6.0]
    chal["fragility"]["paired_sampled"] = True
    chal["fragility"]["loo_drop_max"] = 0.0
    v = rl.lead_is_coin_fragile(chal, live)
    assert v["inside_live_band"] is False
    assert v["fragile"] is True
    assert "SEEDED SAMPLE" in v["reason"]


# ------------------------------------------------- the segment test is not one-sided
def test_a_negative_median_lead_is_refused_by_the_segment_test_itself():
    """(d), alone: every other trigger is switched off.

    live band [10, 40] does not contain -19; loo_drop_max is 0; the challenger
    wins 6 of 8 150d segments (frac 0.75, clearing the old floor). Only (d) can
    refuse this row, so a mutation that removes (d) turns this test red. On
    the real cache the same shape exists (frac 0.88 at lead -18.73pp).
    """
    rl = _rl()
    live = _row(150, 30.0, loo=(10.0, 40.0))
    live["halves"] = [1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0]
    chal = _row(150, 11.0, loo=(0.0, 20.0))
    chal["halves"] = [9.0, 9.0, 9.0, 9.0, 9.0, 9.0, 0.0, 0.0]   # wins 6/8
    chal["fragility"]["lead_interval"] = (-19.0, -15.0)
    chal["fragility"]["loo_drop_max"] = 0.0
    v = rl.lead_is_coin_fragile(chal, live)
    assert v["inside_live_band"] is True       # (a) ALSO fires on this fixture
    assert v["lead_erased_by_one_coin"] is False
    assert v["seg_lead_frac"] == 0.75, v["seg_lead_frac"]
    assert "wins small and loses big" in v["reason"]


# ------------------------------------------------- end-to-end on the real cache
@pytest.fixture(scope="module")
def real():
    rl = _rl()
    out = rl.run()
    live = next(r for r in out["rows"] if r["cfg"] == rl.LIVE)
    best = max(out["rows"], key=rl.score)
    return rl, out, live, best


def test_every_tested_row_carries_a_paired_lead_against_live(real):
    rl, out, live, best = real
    tested = [r for r in out["rows"] if (r.get("fragility") or {}).get("tested")]
    assert len(tested) >= 6
    for r in tested:
        f = r["fragility"]
        assert f.get("lead_vs_live"), r["cfg"]
        assert abs(f["lead_full_universe"]
                   - (r["median"] - live["median"])) < 0.02


def test_a_singleton_challenger_is_not_paired_across_universes(real):
    """A row may only publish a band it can defend.

    The singleton path is the one that matters: `run()` walks sma_r=200 alone,
    so its `per_coin` labels and its published `median` can belong to
    different universe sets (the median is re-derived on the full universe,
    the drops are not). If the band does not bracket its own full-universe
    point the two legs were not measured together, and the row must fall back
    to the raw test instead of publishing a band that cannot be about it.
    """
    rl, out, live, best = real
    withheld = 0
    for r in out["rows"]:
        f = r.get("fragility") or {}
        if not f.get("tested"):
            continue
        assert f.get("lead_full_universe") == round(r["median"] - live["median"], 2)
        if f.get("lead_interval_untested"):
            withheld += 1
            assert f.get("lead_interval") is None, r["cfg"]
            continue
        if f.get("lead_interval") is not None:
            lo, hi = f["lead_interval"]
            assert lo <= f["lead_full_universe"] <= hi, (r["cfg"], lo, hi,
                                                         f["lead_full_universe"])
    # the mechanism must be reachable on this grid, or the test proves nothing
    assert withheld >= 1, ("no row withheld a band — the bracket rule is dead "
                           "code on this cache; check the singleton path")


def test_the_known_universe_mixed_challenger_is_refused_on_the_real_cache(real):
    """sma_r=100 vs LIVE: raw lead outside the live band, paired band straddles
    zero. Before 2026-11-09 this reached the (b)/(c) tests instead."""
    rl, out, live, best = real
    chal = next((r for r in out["rows"]
                 if r["cfg"] == {"topn": 3, "hold": 21, "risk": 0.01,
                                 "sma_r": 100}), None)
    if chal is None or not chal["fragility"].get("lead_interval"):
        pytest.skip("sma_r=100 row not measured on this cache")
    lo, hi = chal["fragility"]["lead_interval"]
    v = rl.lead_is_coin_fragile(chal, live)
    if lo <= 0 <= hi:
        assert v["fragile"] is True
        assert v["inside_basis"] == "paired_leave_one_coin_out"


def test_the_crowned_row_today_is_still_the_incumbent(real):
    rl, out, live, best = real
    v = rl.lead_is_coin_fragile(best, live)
    assert v["is_incumbent"] is True
    assert v["fragile"] is False
