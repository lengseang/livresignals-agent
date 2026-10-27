# -*- coding: utf-8 -*-
"""The research loop must not crown a config whose lead is ONE COIN.

Defect found 2026-10-19 (cron improvement pass):

  * `research_loop.py` ranks configs by `(pos_folds, median)` and gates the
    winner only on rule 3 (beat BTC buy&hold). On 2026-10-19 01:30 it therefore
    announced

        NEW BEST: topn=3 hold=21 risk=0.01 sma_r=200 (median 27.36%, 4/4 folds)
        LIVE EDGE DECAYED: ... sma_r=150 now median 13.7% (4/4)

    to Telegram. The 13.66pp "edge" over the live config is coin luck:
      - leave-one-coin-out (40 draws) on sma_r=200: median 20.92%, low 12.13%,
        mean erosion 7.26pp, max erosion 15.23pp. Removing ANY single coin
        knocks 4.6-14.4pp (mean 12.9) off the lead, and removing SOL alone puts
        200 BELOW the live config (gap +5.6pp vs a -7.4pp live erosion).
      - in the same test the LIVE config is the STABLE one: its leave-one-out
        band (3.58..22.59, mean erosion 0.12pp) CONTAINS its own 13.7%, and
        it stays positive in 4/4 folds. It is not decaying; it is within noise.
      - halving the 4 folds into 8 x 150d segments, sma_r=200 leads 7/8 to
        4/4. A lead that vanishes when one coin is removed and half of it
        sits inside one segment is not a parameter edge.
  * Root cause of the blind spot: `pos_folds` and `median` are properties of
    the SAME four windows every config is scored on, so a challenger that is
    merely a re-slice of one universe wins on both keys. The gate that exists
    (autonomy.cmd_guard) checks sample size and fold COUNT, not fold
    MEMBERSHIP — it is structurally incapable of seeing this, and guessed the
    live config's positive-fold count as `round(4*0.75)=3` instead.

Fix: `collinear_variants()` groups the grid by the ONLY thing `sma_r` changes
(the bull/bear threshold, since the harness skips warm-up per bar) and reports
the live config's leave-one-out band; `lead_is_coin_fragile()` refuses a NEW
BEST when its lead over the live config is (a) inside the live config's own
leave-one-out band, (b) erased by dropping one coin, or (c) mostly carried by
one half of one fold. All three are self-referential (no fitted constant).
The guard is also now fed the LIVE row's real positive-fold count.

A refusal is NOT a decay verdict: LIVE EDGE DECAYED is only emitted when the
live config itself proves fragile by the same tests.
"""
from __future__ import annotations

import importlib.util
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "research_loop", ROOT / "scripts" / "research_loop.py")
rl = importlib.util.module_from_spec(_spec)
sys.modules["research_loop"] = rl
_spec.loader.exec_module(rl)

# ---- the real 2026-10-19 numbers (research/price-cache-1000d.json) ----------
# sma_r=200 minus the live config on every leave-one-coin-out universe.
_SMA200_LEAD = {  # coin dropped -> (200 median, 150 median)
    "SOL": (24.34, 18.71, "SOL"), "SUI": (17.56, 11.65, "SUI"),
    "AAVE": (21.89, 14.19, "AAVE"), "XRP": (12.03, 3.62, "XRP"),
    "ADA": (33.42, 24.79, "ADA"), "BTC": (21.25, 11.96, "BTC"),
    "APT": (22.50, 12.48, "APT"), "LINK": (28.50, 17.24, "LINK"),
    "ARB": (22.56, 11.09, "ARB"), "SEI": (18.84, 7.25, "SEI"),
    "BNB": (25.28, 13.05, "BNB"), "ATOM": (26.96, 14.28, "ATOM"),
    "OP": (24.56, 11.52, "OP"), "DOGE": (28.62, 15.32, "DOGE"),
    "AVAX": (25.88, 12.44, "AVAX"), "ETC": (27.36, 13.70, "ETC"),
    "NEAR": (26.66, 12.81, "NEAR"), "ETH": (26.91, 12.52, "ETH"),
    "FIL": (26.64, 11.91, "FIL"), "DOT": (27.39, 12.51, "DOT"),
    "UNI": (26.71, 7.93, "UNI"), "TIA": (28.12, 8.92, "TIA"),
    "LTC": (34.91, 15.58, "LTC"), "INJ": (29.00, 9.36, "INJ"),
}


def _row(sma_r, median, pos=4, trades=700, rets=None):
    return {"cfg": {"topn": 3, "hold": 21, "risk": 0.01, "sma_r": sma_r},
            "median": median, "pos_folds": pos, "n_folds": 4,
            "trades": trades, "rets": rets or [median] * 4}


def _sma200_fragility():
    """The real 2026-10-19 numbers (40 LOO draws)."""
    return {"sma_r": 200, "median": 27.36, "pos_folds": 4, "n_folds": 4,
            "rets": [28.81, 10.83, 25.9, 30.46], "trades": 736,
            "halves": [14.6, 7.7, -11.7, 12.8, 6.0, 21.2, 14.7, 11.0],
            "fragility": {"tested": True, "draws": 40,
                          "median": 27.36, "loo_median": 20.92,
                          "loo_min": 12.13, "loo_max": 26.41,
                          "loo_drop_mean": 7.26, "loo_drop_max": 15.23,
                          "per_coin": {c: {"ret": a, "pos_folds": 4}
                                       for c, a in ((c, a) for a, b, c in
                                                    _SMA200_LEAD.values())}}}


def _live_fragility():
    """The real live (sma_r=150) numbers. Note the live config's centre is
    NOT the median of its leave-one-out draws — its own 13.7% is inside
    3.58..22.59, i.e. inside its noise, while the challenger's is not."""
    return {"sma_r": 150, "median": 13.7, "pos_folds": 4, "n_folds": 4,
            "rets": [5.8, 1.26, 22.89, 21.59], "trades": 697,
            "halves": [-2.1, 6.7, -13.5, 13.1, -0.7, 26.8, 4.4, 9.2],
            "fragility": {"tested": True, "draws": 40,
                          "median": 13.7, "loo_median": 14.23,
                          "loo_min": 3.58, "loo_max": 22.59,
                          "loo_drop_mean": 0.12, "loo_drop_max": 10.12,
                          "per_coin": {c: {"ret": b, "pos_folds": 4}
                                       for c, b in ((c, b) for a, b, c in
                                                    _SMA200_LEAD.values())}}}


# ---------------------------------------------------------------- unit: demote
def test_live_config_is_not_called_decayed_on_a_noise_lead(monkeypatch):
    """The real 2026-10-19 row pair: the refusal must not emit a decay verdict."""
    live = _live_fragility()
    lead = _sma200_fragility()
    verdict = rl.lead_is_coin_fragile(lead, live, n_folds=4)
    assert verdict["fragile"] is True
    assert verdict["reason"], "a refusal must SAY why"
    assert verdict["live_fragile"] is False, (
        "the live config's own leave-one-out band contains its median — "
        "it is not decaying, it is within noise")
    # and the LIVE EDGE DECAYED line must therefore be suppressed
    changed = rl._change_lines(
        prev={"rows": [_row(150, 12.0), _row(200, 24.0)]},
        cur={"ts": "2026-10-19T01:30:00+00:00",
             "rows": [lead, live],
             "bench_btc_median": -24.26, "bench_eql_median": -52.9},
        best=lead, live_row=live)
    assert not any("DECAYED" in c for c in changed), changed
    assert any("REFUSED" in c for c in changed), changed
    # no ANNOUNCEMENT: the only line mentioning NEW BEST must be the refusal
    assert not any(c.startswith("NEW BEST") for c in changed), changed
    assert len(changed) == 1, changed


def test_missing_leave_one_out_data_is_not_a_pass():
    v = rl.lead_is_coin_fragile(_row(200, 30.0), _row(150, 10.0), n_folds=4)
    assert v["fragile"] is False
    assert v["live_fragile"] is False
    assert "untested" in v["basis"]


def test_a_lead_that_survives_every_drop_is_admitted():
    lead = _sma200_fragility()
    # every drop still leaves the challenger 40pp ahead
    lead["fragility"]["per_coin"] = {
        c: {"ret": d["ret"] + 20, "pos_folds": 4}
        for c, d in lead["fragility"]["per_coin"].items()}
    lead["fragility"]["loo_drop_max"] = 0.0
    lead["fragility"]["loo_min"] = 47.36
    live = _live_fragility()
    live["fragility"]["loo_min"] = 5.0
    live["fragility"]["loo_max"] = 9.0
    v = rl.lead_is_coin_fragile(lead, live, n_folds=4)
    assert v["fragile"] is False, v


def test_a_live_config_that_is_itself_fragile_does_emit_decay():
    live = _live_fragility()
    live["fragility"]["loo_min"] = 20.0      # live median 13.7 is BELOW its band
    v = rl.lead_is_coin_fragile(_sma200_fragility(), live, n_folds=4)
    assert v["live_fragile"] is True


def _clean_pair(lead_gap=14.0, drop=3.0):
    """A challenger with a clean lead: live band narrow and far below it,
    one-coin drops small, 7/8 segments won. Each trigger can then be
    switched on ALONE, so a mutation that removes one is caught."""
    live = _live_fragility()
    live["median"] = 10.0
    live["fragility"]["loo_min"] = 8.0
    live["fragility"]["loo_max"] = 12.0
    lead = _sma200_fragility()
    lead["median"] = 10.0 + lead_gap
    lead["fragility"]["median"] = lead["median"]
    lead["fragility"]["loo_min"] = lead["median"] - 2.0
    lead["fragility"]["loo_max"] = lead["median"]
    lead["fragility"]["per_coin"] = {
        c: {"ret": lead["median"] - drop, "pos_folds": 4}
        for c in lead["fragility"]["per_coin"]}
    lead["fragility"]["loo_drop_max"] = drop
    return lead, live


def test_baseline_clean_lead_is_admitted():
    lead, live = _clean_pair()
    v = rl.lead_is_coin_fragile(lead, live, n_folds=4)
    assert v["fragile"] is False, v
    assert v["live_fragile"] is False


def test_one_coin_erasure_alone_is_enough_to_refuse():
    """Only trigger (b): the lead is inside neither band's width rule and
    every segment is won — but dropping ONE coin wipes the lead out."""
    lead, live = _clean_pair(lead_gap=14.0, drop=15.0)
    v = rl.lead_is_coin_fragile(lead, live, n_folds=4)
    assert v["inside_live_band"] is False
    assert v["lead_erased_by_one_coin"] is True
    assert v["seg_lead_frac"] == 0.75
    assert v["fragile"] is True, v
    assert "alone cuts" in v["reason"]


def test_one_hot_quarter_alone_is_enough_to_refuse():
    """Only trigger (c): the lead survives every drop, but it lives in <75%
    of the 150d segments."""
    lead, live = _clean_pair(lead_gap=14.0, drop=3.0)
    live["halves"] = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
    lead["halves"] = [30.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 1.0]
    v = rl.lead_is_coin_fragile(lead, live, n_folds=4)
    assert v["inside_live_band"] is False
    assert v["lead_erased_by_one_coin"] is False
    assert v["seg_lead_frac"] < 0.75
    assert v["fragile"] is True, v
    assert "segments" in v["reason"]


def test_lead_that_is_no_bigger_than_the_live_configs_noise_is_refused():
    """Only trigger (a): the lead survives every single-coin drop and is
    consistent across segments — but it is narrower than the live config's
    OWN leave-one-coin-out band, so it cannot be told apart from noise."""
    lead, live = _clean_pair(lead_gap=4.0, drop=1.0)
    live["fragility"]["loo_min"] = 1.0
    live["fragility"]["loo_max"] = 22.59     # a wide incumbent band
    v = rl.lead_is_coin_fragile(lead, live, n_folds=4)
    assert v["lead_erased_by_one_coin"] is False
    assert v["seg_lead_frac"] == 0.75
    assert v["inside_live_band"] is True
    assert v["fragile"] is True, v
    assert "leave-one-coin-out band" in v["reason"]


# ---------------------------------------------------------------- unit: pair
def test_collinear_variants_groups_by_the_effective_parameter():
    rows = [_row(200, 27).copy(), _row(150, 13).copy(), _row(100, 26).copy(),
            _row(150, 9.5).copy()]
    rows[1]["cfg"]["hold"] = 30          # hold does NOT change the sma_r family
    rows[2]["cfg"]["topn"] = 5
    fams = rl.collinear_variants(rows)
    keyed = sorted((r["cfg"].get("sma_r"), r["cfg"].get("topn")) for r in fams[150])
    assert keyed == [(150, 3), (150, 3)], fams[150]
    assert len(fams[200]) == 1 and len(fams[100]) == 1


def test_lead_interval_is_the_union_of_every_variant_band():
    lo, hi = rl._lead_interval({"sma_r": 200, "fragility":
                                {"median": 27.0,
                                 "per_coin": {"A": {"ret": 17.0},
                                              "B": {"ret": 24.0}}}})
    assert (lo, hi) == (3.0, 10.0)
    assert rl._lead_interval({"fragility": {}}) is None


def test_segment_lead_fraction_reads_the_halves():
    # 2 of 4 segments prefer the challenger; a near-tie still counts as one
    assert rl._seg_lead_frac([20.0, 8.0, 9.5, 2.0],
                             [10.0, 9.0, 9.0, 5.0]) == 0.5
    # a tie is NOT a win for the challenger
    assert rl._seg_lead_frac([1.0, 1.0], [1.0, 0.0]) == 0.5
    # the real pair on the real data: 6/8 segments, not "4/4 folds"
    assert rl._seg_lead_frac(_sma200_fragility()["halves"],
                             _live_fragility()["halves"]) == 0.75
    assert rl._seg_lead_frac(None, [10.0, 9.0]) is None


# ------------------------------------------------- end-to-end: real harness
def test_run_measures_fragility_on_every_multi_variant_family():
    """The unit tests pass if the guard is never CALLED. This one drives the
    real harness (6-20s) and asserts the run actually attached a leave-one-
    coin-out block to the live config — a config that is 'untested' would be
    admitted by lead_is_coin_fragile(), i.e. the guard would be dead code."""
    cur = rl.run()
    live = next(r for r in cur["rows"] if r["cfg"] == rl.LIVE)
    frag = live.get("fragility") or {}
    assert frag.get("tested") is True, frag
    assert frag["draws"] >= 1
    assert frag["per_coin"], "leave-one-coin-out must name the coins it dropped"
    assert live.get("halves") and len(live["halves"]) == 2 * live["n_folds"]
    # every sma_r variant in the live family is measured too, or the
    # head-to-head comparison has one untested side
    fam = rl.collinear_variants(cur["rows"])[150]
    assert len(fam) >= 2
    assert all((r.get("fragility") or {}).get("tested") for r in fam), fam


def test_the_solo_challenger_is_measured_even_when_it_has_no_family():
    """The 2026-10-19 grid: sma_r=200 is the only 200 in the grid, so its
    family has ONE member. It is also the config the loop crowns. Skipping
    singletons left the winner 'untested' — and an untested row is admitted
    by lead_is_coin_fragile() by construction, so the refusal was dead code.

    ⚠️ REWRITTEN 2026-10-20. As originally written this asserted
    `live_fragile is False` on the LIVE data. That held on the 10-19 cache and
    stopped holding after the 01:00 refresh (LIVE sma_r=150 median 13.7% →
    25.11%, its own LOO band [1.30, 23.18] no longer contains it, and the
    crowned config moved to sma_r=100). A test that pins a verdict about
    TOMORROW's data goes red for a reason that is not a defect — on a monitor
    carousel that trains the reader to ignore red. The 10-19 numbers are pinned
    as a unit test with a literal fixture
    (`test_live_config_is_not_called_decayed_on_a_noise_lead`); this one now
    asserts the properties that must hold on WHATEVER the cache says today.
    """
    cur = rl.run()
    best = max(cur["rows"], key=rl.score)
    assert len(rl.collinear_variants(cur["rows"])[best["cfg"]["sma_r"]]) == 1
    frag = best.get("fragility") or {}
    assert frag.get("tested") is True, frag
    assert frag["per_coin"]
    # and the verdict on the real numbers must be a REFUSAL, not "untested"
    live = next(r for r in cur["rows"] if r["cfg"] == rl.LIVE)
    v = rl.lead_is_coin_fragile(best, live, n_folds=4)
    assert v["basis"] != "fragility untested (no leave-one-coin-out data)", v
    assert v["fragile"] is True, v
    # live_fragile is DERIVED from today's band, not pinned. Its contract:
    # the LIVE median is OUTSIDE its own leave-one-coin-out band on EITHER
    # side (below = decay, above = regime break). Pin the relationship, not
    # the verdict, so a cache refresh cannot red-line this test for a reason
    # that is not a defect — and separate unit tests pin the wording.
    lf = live.get("fragility") or {}
    med = live["median"]
    outside = not (lf["loo_min"] <= med <= lf["loo_max"])
    assert v["live_fragile"] is outside, (
        f"live_fragile={v['live_fragile']} but LIVE median {med} "
        f"vs its own LOO band [{lf['loo_min']}, {lf['loo_max']}]")
    lines = rl._change_lines(
        # `prev` must be a benchmark that is UNSCORED by the live row, or the
        # decay branch is unreachable by construction (see the comment there).
        # It used to be `_row(150, 12.0)` — the LIVE cfg at an invented 12.0%.
        # Once the 2026-10-27 cache moved LIVE to 25.32%, `score(live) <
        # score(prev)` was False and no DECAYED line could be emitted, so the
        # direction assertion below went red for a fixture reason, not a
        # defect. A HIGHER invented bar keeps the branch reachable whatever
        # the cache says, which is the property this test is about.
        prev={"rows": [_row(150, live["median"] + 10.0)]},
        cur={"ts": "x", "rows": [best, live],
             "bench_btc_median": 0, "bench_eql_median": 0},
        best=best, live_row=live)
    if not v["live_fragile"]:
        assert not any("DECAYED" in c for c in lines), (
            "LIVE is inside its own band, so no DECAYED line may be emitted")
    else:
        decay = [c for c in lines if "DECAYED" in c]
        assert decay, "LIVE is outside its band but no line says so"
        # and the line must name the direction that is actually true
        if med < lf["loo_min"]:
            assert "floor" in decay[0]
        else:
            assert "ceiling" in decay[0] and "floor" not in decay[0], (
                f"LIVE is ABOVE its ceiling but the line claims a floor: {decay[0]}")
