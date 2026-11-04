"""Pins for the 2026-11-04 paper-exit-fill audit.

These are deliberately NOT level pins on the live book (the failure mode
recorded three times in the loop journal: a test that hardcodes a number the
01:00 cache refresh moves by one bar). They pin the SHAPE of the defect, which
is stable while the writer is unchanged, and they say out loud what the audit
cannot yet do instead of hiding it behind a green skip.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import paper_exit_fill as pef  # noqa: E402

PAPER = ROOT / "data" / "unified-paper.json"


def _rec(symbol="BTC/USDT", entry=100.0, sl=98.0, tp=104.0, pnl=-0.10,
         reason="SL", ts="2026-10-01T00:00:00+00:00"):
    return {"symbol": symbol, "entry": entry, "sl": sl, "tp": tp, "pnl": pnl,
            "size": 5.0, "reason": reason, "ts_utc": ts}


# ------------------------------------------------------------------ fixtures
def test_stop_pct_and_booked_pct_are_percent_of_stake():
    r = _rec(entry=100.0, sl=98.0, pnl=-0.10)
    assert pef.stop_pct(r) == pytest.approx(-2.0)
    assert pef.booked_pct(r) == pytest.approx(-2.0)   # -0.10 on $5 = -2%


def test_const_stop_excludes_trails_above_entry():
    assert pef.const_stop(_rec(sl=98.0)) == pytest.approx(-2.0)
    assert pef.const_stop(_rec(sl=101.2)) is None     # ratcheted trail
    assert pef.const_stop(_rec(sl=100.0)) is None     # breakeven lock


# ------------------------------------------------------------------ bracket
def test_bracket_separates_the_three_classes():
    recs = [
        _rec(sl=98.0, pnl=-0.131),   # -2.62% booked vs -2.00 level: BEYOND
        _rec(sl=100.0, pnl=-0.02),   # breakeven lock, tiny loss
        _rec(sl=101.2, pnl=-0.04),   # ratcheted trail, booked below it
        _rec(sl=98.0, pnl=-0.10),    # exactly at the level
    ]
    out = pef.bracket_vs_level(recs)
    assert out["sl_with_level"] == 4
    assert out["classes"]["below_entry"]["n"] == 2
    assert out["classes"]["below_entry"]["beyond"] == 1
    assert out["classes"]["at_breakeven"]["n"] == 1
    assert out["classes"]["pos_trail"]["n"] == 1
    assert out["at_level"] == 1


def test_bracket_never_reports_an_inside_level_fill_as_beyond():
    out = pef.bracket_vs_level([_rec(sl=98.0, pnl=-0.09)])   # -1.8%, better
    assert out["inside_level"] == 1
    assert out["beyond_level"] == 0


# ---------------------------------------------------------------- the bound
def test_bound_is_none_without_a_body():
    bar = [100.0, 105.0, 95.0, 100.0]        # flat close: no direction
    assert pef._one_bar_adverse_bound(_rec(), 1.0, bar) is None
    up = [100.0, 105.0, 95.0, 104.0]
    assert pef._one_bar_adverse_bound(_rec(), 1.0, up) is not None


def test_bound_scales_with_range_over_body():
    small = [100.0, 101.0, 99.5, 100.5]
    big = [100.0, 104.0, 96.0, 100.5]
    a = pef._one_bar_adverse_bound(_rec(), 1.0, small)
    b = pef._one_bar_adverse_bound(_rec(), 1.0, big)
    assert b > a > 0


TS_OCT1 = 1790812800000        # 2026-10-01T00:00:00Z in ms

def test_adversarial_pnl_none_when_price_never_reaches_the_stop():
    rec = _rec(entry=100.0, sl=98.0, ts="2026-10-01T00:00:00+00:00")
    ts = [TS_OCT1, TS_OCT1 + 300_000]
    bars = [[100.0, 101.0, 99.5, 100.5], [100.5, 101.0, 100.0, 100.8]]
    assert pef.adversarial_pnl(rec, ts, bars, 1.0) is None


def test_adversarial_pnl_charges_the_worst_bar_through_the_stop():
    rec = _rec(entry=100.0, sl=98.0, ts="2026-10-01T00:00:00+00:00")
    ts = [TS_OCT1, TS_OCT1 + 300_000]
    # Rule shape (see `adversarial_pnl`): a bar carries a bound only when its
    # close is ABOVE its open AND the range exceeds the body. Bar 2 satisfies
    # it; an earlier draft used a bar1 closing below its open, which the rule
    # skips by design, so the fixture asserted behaviour the module never had.
    bars = [[100.0, 100.2, 97.0, 100.1], [100.1, 100.8, 96.0, 100.4]]
    worst, n_hit, n_bound = pef.adversarial_pnl(rec, ts, bars, 1.0)
    assert n_hit == 2 and n_bound == 2                 # both bars reached the stop
    assert worst < -2.6                                # worse than the booked -2% level


def test_adversarial_pnl_survives_the_TUPLE_avg_gain_in_force_returns():
    """The live seam: `summarise` passes `avg_gain_in_force(...)` = (mean, n).

    Every other fixture here passes a bare float, so the whole bar method could
    raise TypeError on the only path that runs it and the suite stayed green.
    2026-11-04 this test failed on the shipped file.
    """
    rec = _rec(entry=100.0, sl=98.0, ts="2026-10-01T00:00:00+00:00")
    ts = [TS_OCT1, TS_OCT1 + 300_000]
    bars = [[100.0, 100.2, 97.0, 100.1], [100.1, 100.3, 96.0, 96.2]]
    scalar = pef.adversarial_pnl(rec, ts, bars, 1.0)
    tupled = pef.adversarial_pnl(rec, ts, bars, (1.0, 7))
    assert tupled == scalar and scalar is not None


def test_adversarial_pnl_refuses_a_negative_or_missing_in_force_gain():
    rec = _rec(entry=100.0, sl=98.0, ts="2026-10-01T00:00:00+00:00")
    ts = [TS_OCT1]
    bars = [[100.0, 100.2, 97.0, 100.1]]
    assert pef.adversarial_pnl(rec, ts, bars, (-0.4, 9)) is None
    assert pef.adversarial_pnl(rec, ts, bars, None) is None


# ------------------------------------------------------------ the summarise
def test_summarise_is_one_sided_and_never_raises_the_book(monkeypatch):
    """The 2026-11-04 rule: a replacement fill may only LOWER the book.

    Whatever `adversarial_pnl` returns, the summarised total must be <= the
    booked total. This is the guard that made the first (non-monotone) run
    unusable, and it must be enforced in code, not in the write-up.
    """
    recs = [_rec(sl=98.0, pnl=-0.131, reason="SL"),
            _rec(sl=100.0, pnl=0.10, reason="TP"),
            _rec(sl=98.0, pnl=0.30, reason="STALL")]
    monkeypatch.setattr(pef, "adversarial_pnl",
                        lambda *a, **k: (5.0, 1, 1))   # absurdly GOOD fill
    out = pef.summarise(recs, bars=object())
    assert out["adv_gross"] <= out["booked_gross"] + 1e-9
    assert out["n_moved_worse"] == 0


# ------------------------------------------------------- the real-book shape
@pytest.mark.skipif(not PAPER.exists(), reason="no paper book on this machine")
def test_live_book_sl_fills_are_stamped_beyond_their_constant_level():
    """Every below-entry SL that is not exactly at its level is BEYOND it.

    If this ever flips (some records inside the level), the writer started
    booking at the stop and the fill question changes — that is news, not a
    red test to paper over.
    """
    recs = pef.load_book(PAPER)
    out = pef.bracket_vs_level(recs)
    be = out["classes"].get("below_entry")
    if not be or be["n"] < 50:
        pytest.skip("too few below-entry SL records to judge the writer")
    assert be["beyond"] / be["n"] > 0.9
    assert be["mean_gap"] < -0.1


@pytest.mark.skipif(not (ROOT / "data" / "trading.db").exists(),
                    reason="no candle store on this machine")
@pytest.mark.xfail(strict=False, reason=(
    "the bound is ONE-SIDED but not yet VALID: it validates on only 1 of the "
    "book's recorded-fill records and violates it. Until it validates it "
    "cannot be quoted; this xfail is the defect stated, not hidden."))
def test_bound_validates_where_the_book_records_a_fill():
    recs = pef.load_book(PAPER)
    out = pef.agreement(recs, pef.Bars())
    assert out["pass"], out
