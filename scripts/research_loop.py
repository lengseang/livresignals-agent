#!/usr/bin/env python3
"""NIGHTLY RESEARCH LOOP — keeps hunting, reports only what changed.

Runs the walk-forward + robustness harness, compares against the config
currently deployed live, and writes a findings file. Pings Telegram only
when something ACTUALLY changed (a better config appears, or the edge
decays). Silence = nothing to report, which is the correct default.

State: data/research-loop.json   Log: logs/research-loop.log
"""
from __future__ import annotations

import importlib.util
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
RES = BASE / "research"
STATE = BASE / "data" / "research-loop.json"
FINDINGS = BASE / "reports" / "research-findings.md"

# config currently deployed live in scripts/regime_trader.py
LIVE = {"topn": 3, "hold": 21, "risk": 0.01, "sma_r": 150}

# The parameters that actually partition OUTCOMES. `bt()` skips warm-up on a
# per-bar basis (`range(max(start, sma_r + 25), end)`), so sma_r only moves the
# bull/bear threshold and two sma_r values walk ENTIRELY DIFFERENT trades —
# while hold / topn / risk merely re-slice whatever trades the threshold picked.
# Ranking across this key compares noise; see collinear_variants().
FRAGILITY_BY = ("sma_r",)
# Leave-one-coin-out: drop EVERY non-BTC coin once, no sampling.
#
# This used to be "12 seeded draws", and on 2026-11-03 those 12 draws were
# found to collapse to 8 distinct universes over 23 coins (DOGE 3x, SOL 2x,
# LTC 2x, five once). The band is a min/max, so both tails were then decided
# by whichever coin the shuffle happened to land on: floor 7.47 = three copies
# of the DOGE draw, ceiling 30.20 = the LTC draw. Cost of exhausting it is the
# same order as the sampled version (23 x 4 folds vs 12 x 4) and it is
# re-derived on every run, so there is no reason to sample a population this
# small. `draws` is kept in the payload as the number of universes measured.
LOO_DRAWS = None          # None = exhaustive; an int forces the old sampling


def load_harness():
    spec = importlib.util.spec_from_file_location("wf6", RES / "06-walkforward.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["wf6"] = m
    spec.loader.exec_module(m)
    return m


def run():
    h = load_harness()
    folds = h.folds
    grid = [
        {"topn": 3, "hold": 21, "risk": 0.01, "sma_r": 200},
        {"topn": 3, "hold": 21, "risk": 0.01, "sma_r": 150},   # live
        {"topn": 3, "hold": 21, "risk": 0.01, "sma_r": 100},
        {"topn": 3, "hold": 14, "risk": 0.01, "sma_r": 150},
        {"topn": 3, "hold": 30, "risk": 0.01, "sma_r": 150},
        {"topn": 3, "hold": 21, "risk": 0.02, "sma_r": 150},
        {"topn": 5, "hold": 21, "risk": 0.01, "sma_r": 150},
        {"topn": 4, "hold": 21, "risk": 0.015, "sma_r": 120},
        {"topn": 2, "hold": 21, "risk": 0.01, "sma_r": 150},
    ]
    rows = []
    for cfg in grid:
        out = [h.bt(a, b, "regime", **cfg) for a, b in folds]
        rets = [o[0] for o in out]
        ntrades = sum(o[1] for o in out)
        rows.append({
            "cfg": cfg,
            "rets": [round(r, 2) for r in rets],
            "halves": _halves(h, cfg),
            "median": round(statistics.median(rets), 2),
            "pos_folds": sum(1 for r in rets if r > 0),
            "n_folds": len(folds),
            "trades": ntrades,
        })
    bench_btc = [h.bh("BTC", a, b) for a, b in folds]
    bench_eql = [sum(h.bh(c, a, b) for c in h.coins) / len(h.coins)
                 for a, b in folds]
    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
        "bench_btc_median": round(statistics.median(bench_btc), 2),
        "bench_eql_median": round(statistics.median(bench_eql), 2),
        "live": LIVE,
    }
    # Every sma_r variant got the SAME walk-forward windows, so fold COUNT and
    # fold coverage cannot separate them. Measure the thing that can: does the
    # lead survive losing a coin, and does it live in one half of one fold?
    # NOTE: collinear_variants() returns {family_key: [rows]}; iterate .items()
    # — iterating the dict yields keys and silently marks every family untested
    # (caught by test_run_measures_fragility_on_every_multi_variant_family).
    #
    # A family with ONE member is still measured: its leave-one-coin-out band
    # is what the OTHER families are compared against, and an untested row is
    # admitted by lead_is_coin_fragile() by construction. On the 2026-10-19
    # grid sma_r=200 (the challenger) is alone while sma_r=150 has six rows;
    # skipping singletons left the best config permanently "untested" and the
    # refusal dead — the exact shape of bug this guard exists to stop.
    fams = collinear_variants(rows)
    for key, fam in fams.items():
        multi = len(fam) >= 2
        for r in fam:
            if not multi and r is not _best_row(rows):
                # singletons that cannot be challenged need no extra walk
                r["fragility"] = {"tested": False, "n_variants": len(fam)}
                continue
            try:
                r["fragility"] = coin_fragility(h, r, LOO_DRAWS)
                r["fragility"]["n_variants"] = len(fam)
            except Exception as e:  # noqa: BLE001 — never kill the loop
                r["fragility"] = {"tested": False, "error": str(e)[:80],
                                  "n_variants": len(fam)}
    # THE PAIRED LEAD (2026-11-09). `lead_is_coin_fragile` needs
    # `chal.drop(c) - live.drop(c)` — one universe, one difference — but the
    # challenger is only known after the whole grid is scored. Every tested
    # row already carries `per_coin`, so the pairing is a lookup, not another
    # walk of 23 universes: cost is the 9 rows x 4 folds above, unchanged.
    # Rows are paired with the LIVE row by family only where the universe
    # matters — the key is just "which coin was dropped", and every tested
    # row dropped the same 23 coins, so a cross-family pair is well defined.
    live_cfg = next((r for r in rows
                     if all(r["cfg"].get(k) == v for k, v in LIVE.items())), None)
    lp = ((live_cfg or {}).get("fragility") or {})
    for r in rows:
        f = r.get("fragility") or {}
        if not f.get("tested") or not lp.get("tested"):
            continue
        mine, theirs = f.get("per_coin") or {}, lp.get("per_coin") or {}
        paired = [round(mine[c]["ret"] - theirs[c]["ret"], 2)
                  for c in mine if c in theirs]
        if not paired:
            continue
        own = f.get("median")
        # The published head-to-head lead must be one point of this band, or
        # the band was built against a different universe set.
        if own is not None:
            f["lead_full_universe"] = round(own - lp.get("median", 0), 2)
        paired.sort()
        # The full-universe point is carried SEPARATELY (not inside the band):
        # a 23-draw band plotted around the full-universe lead is a
        # restatement of it, and including it lets a crafted/mismatched band
        # pass the "does it contain the published lead" check for free.
        f["lead_vs_live"] = paired
        # A seeded-sample band is NOT a leave-one-coin-out population: it must
        # be able to REFUSE (inside its own noise), never to ADMIT. Same rule
        # as `lead_is_coin_fragile`'s "(a)" test; both read this flag.
        f["paired_sampled"] = bool(f.get("sampled") or lp.get("sampled"))
        if f["paired_sampled"]:
            f.pop("lead_interval", None)
        else:
            # A band must BRACKET its own full-universe point. When it does
            # not, the drops and the median came from different universe sets
            # — the singleton case above — and publishing the band would be
            # publishing a number about a different comparison. Withhold it
            # and let `lead_is_coin_fragile` fall back to the raw test, which
            # is at least honest about what it compares.
            lead = f.get("lead_full_universe")
            if lead is not None and paired[0] - 0.01 <= lead <= paired[-1] + 0.01:
                f["lead_interval"] = (paired[0], paired[-1])
            else:
                f["lead_interval_untested"] = (
                    f"paired band [{paired[0]:+.2f}, {paired[-1]:+.2f}] does not "
                    f"bracket its own full-universe lead {lead:+.2f}pp — "
                    f"the drops and the median came from different universes")
    return out


def _best_row(rows):
    """The row the loop will actually crown — must be measured, even solo."""
    return max(rows, key=score, default=None) if rows else None


def _halves(h, cfg):
    """Each 300d walk-forward fold split in half -> 2N x 150d segments.

    A lead concentrated in a quarter of the data is a period, not a parameter.
    Returns None when the harness cannot be split (so callers skip the test
    rather than treating it as a pass).
    """
    try:
        segs = []
        for a, b in h.folds:
            mid = a + (b - a) // 2
            segs += [(a, mid), (mid, b)]
        return [round(h.bt(a, b, "regime", **cfg)[0], 2) for a, b in segs]
    except Exception:  # noqa: BLE001
        return None


def coin_fragility(h, r, draws=None):
    """Leave-one-coin-out: how much of this config's median depends on ONE coin?

    `bt()` reads the universe from module state (`h.coins`) while sma()/sd()
    take prices by symbol, so dropping a coin is exactly `h.coins = sub`.

    DEFAULT IS EXHAUSTIVE: every non-BTC coin is dropped once. The old
    behaviour (`draws=12`) sampled the same space with `random.Random(i)` and
    the 2026-11-03 run showed 12 draws = 8 universes, so the min/max band was
    a property of the seed. `draws=<int>` still forces the sampled mode (kept
    so the sampling behaviour itself stays testable); the payload says which
    mode produced it in `basis`, because a reader cannot otherwise tell an
    exhaustive band from an 8-of-23 one.
    """
    import random

    full_coins = list(h.coins)
    med = round(statistics.median(
        [h.bt(a, b, "regime", **r["cfg"])[0] for a, b in h.folds]), 2)
    base = [c for c in full_coins if c != "BTC"]
    if draws is None:
        dropped_list = list(base)              # one universe per coin
        basis = f"exhaustive leave-one-coin-out over {len(base)} coins"
    else:
        dropped_list = []
        for i in range(int(draws)):
            sub = base[:]
            random.Random(i).shuffle(sub)
            dropped_list.append(sub[-1])
        basis = (f"{len(dropped_list)} seeded draws over {len(base)} coins "
                 f"({len(set(dropped_list))} distinct universes)")
    meds, per_coin = [], {}
    try:
        for dropped in dropped_list:
            h.coins = sorted(c for c in base if c != dropped)
            rets = [h.bt(a, b, "regime", **r["cfg"])[0] for a, b in h.folds]
            m = statistics.median(rets)
            meds.append(m)
            per_coin[dropped] = {"ret": round(m, 2), "pos_folds":
                                 sum(1 for x in rets if x > 0)}
    finally:
        h.coins = full_coins
    if not meds:
        return {"tested": False, "n_variants": 0}
    return {"tested": True, "draws": len(meds),
            "distinct_universes": len(set(dropped_list)),
            "basis": basis,
            # `sampled` decides whether a PAIRED lead band is a population or a
            # seeded sample; a sampled band may sit inside the gate, a sampled
            # verdict may not (see lead_is_coin_fragile).
            "sampled": draws is not None,
            "median": med,
            "loo_median": round(statistics.median(meds), 2),
            "loo_min": round(min(meds), 2), "loo_max": round(max(meds), 2),
            "loo_drop_mean": round(med - statistics.median(meds), 2),
            "loo_drop_max": round(med - min(meds), 2),
            "per_coin": per_coin}


def collinear_variants(rows):
    """Group rows by the parameter(s) that change the traded population.

    Two rows with different `hold` share their sma_r family: they traded the
    same names in the same regime, so their medians are not independent
    evidence. Returns [(key_value, [rows...])] as a dict keyed by the value.
    """
    fams = {}
    for r in rows:
        key = tuple(r["cfg"].get(k) for k in FRAGILITY_BY)
        fams.setdefault(key[0] if len(key) == 1 else key, []).append(r)
    return fams


def _lead_interval(row):
    """Every (challenger - its leave-one-coin-out variants) difference.

    A single median hides a sign flip: sma_r=200 beat the live config by
    +13.66pp on the full universe; dropping any single coin moved its own
    median by -4.6..-14.4pp. The interval is the honest width of the number
    being announced. Returns (lo, hi) of the DROPS, or None when untested.
    """
    frag = row.get("fragility") or {}
    own = frag.get("median")
    drops = [own - v["ret"] for v in (frag.get("per_coin") or {}).values()
             if isinstance(v, dict) and v.get("ret") is not None
             and own is not None]
    return (round(min(drops), 2), round(max(drops), 2)) if drops else None


def _seg_lead_frac(chal_halves, live_halves):
    """Fraction of 150d segments the challenger genuinely out-earns the live
    config on. 4/4 folds can still be 5/8 segments. None when unsplittable.

    Ties count as NOT preferred: a segment where both configs earn the same
    is not evidence for the challenger.
    """
    if not chal_halves or not live_halves:
        return None
    if len(chal_halves) != len(live_halves):
        return None
    won = sum(1 for a, b in zip(chal_halves, live_halves) if a > b)
    return round(won / len(chal_halves), 3)


def _row_like(best):
    """Accept either a full row (has `cfg`) or a compact {pos_folds, median}.

    A compact row has no config to name, so it is NOT labelled as the live
    config — that would attribute a challenger's numbers to what is running.
    """
    if "cfg" in (best or {}):
        return best
    return {"cfg": None, "median": best["median"],
            "pos_folds": best["pos_folds"],
            "n_folds": best.get("n_folds", 4),
            "halves": best.get("halves"),
            "fragility": best.get("fragility") or {}}


def lead_is_coin_fragile(chal, live_row, n_folds=4):
    """Would this NEW BEST survive losing a coin, or is it one name's luck?

    Fragile when ANY of these hold:
      a) the challenger's OWN leave-one-coin-out band spans its head-to-head
         lead over the live config (the "lead" is inside the challenger's noise);
      b) the lead is ERASED by dropping the single coin the result depends on;
      c) < 75% of 150d segments prefer the challenger (one hot quarter).
    `live_fragile` is a SEPARATE question: is the live config's own median
    below its leave-one-out band? Only then is a decay verdict honest — a
    challenger being lucky is not evidence the incumbent is decaying.
    Returns a dict; never raises on untested rows (they carry an explicit basis).
    """
    cf = (chal or {}).get("fragility") or {}
    lf = (live_row or {}).get("fragility") or {}
    # THE INCUMBENT IS NOT A CHALLENGER. `best_row` is max(rows, key=score)
    # and the LIVE config is one of the rows, so on any run where the
    # incumbent is still best (or merely TIES a challenger, max() takes the
    # first in grid order) `chal is live_row` and every test below fires on
    # itself: lead is 0.00pp by construction, 0.00 is inside the live band,
    # 0 - drop_max <= 0 always, and a row cannot prefer itself in 75% of
    # segments. The run then refuses the incumbent in one line and calls it
    # the incumbent in the next — the 2026-10-24 contradiction. The
    # `live_fragile` question below is the only one that can be asked of
    # this row, and it is asked.
    incumbent = chal is live_row
    if not cf.get("tested") or not lf.get("tested"):
        return {"fragile": False, "live_fragile": False,
                "is_incumbent": incumbent,
                "basis": "fragility untested (no leave-one-coin-out data)",
                "reason": ""}

    lead = round((chal.get("median", 0) - live_row.get("median", 0)), 2)
    live_band = (lf["loo_min"], lf["loo_max"])
    chal_band = (cf["loo_min"], cf["loo_max"])

    # (a) the challenger's lead survives losing a coin from BOTH sides at once
    # THE PAIRED LEAD (2026-11-09). Until today this test asked whether the
    # RAW head-to-head lead (each config on the full universe) sat inside the
    # live config's leave-one-coin-out band. That mixes two universes: the
    # challenger's number is full-universe, the band is not. A band only says
    # something about a lead if both legs are measured on the SAME universe.
    # `per_coin` already gives both legs (each row drops every coin), so the
    # paired lead is `chal.drop(c) - live.drop(c)` per coin — one population,
    # one difference — and its spread is the honest width of the lead.
    # Measured 2026-11-09 on the live cache (sma_r=100 vs LIVE 150): raw lead
    # -3.63 vs the live band [10.68, 44.55] -> "not inside" -> the gate passed
    # a challenger whose paired lead is -13.17..+7.12 (median -1.86): it
    # LOSES under 15 of 23 drops, i.e. the lead is inside its own noise. The
    # old form also called a lead of +1.0 "admitted" when every config was
    # 30pp, i.e. inside a band that says nothing about a 1pp lead.
    # `lead_interval` is optional: rows from an older caller/serializer lack
    # it, and absent means the paired test cannot run — say so via
    # `paired_untested` rather than falling back silently to the raw test.
    # THE SAMPLED ASYMMETRY (2026-11-09): a seeded-draw band is not a
    # population, so it may REFUSE (inside its own noise) but must never
    # ADMIT. `run()` therefore also emits `lead_vs_live` for sampled rows with
    # no `lead_interval`: the gate below still uses the band, and the
    # admission in (b)/(c) stays blocked by `paired_untested`.
    paired = cf.get("lead_interval")
    paired_band = None
    sampled = bool(cf.get("paired_sampled") or cf.get("sampled")
                   or lf.get("sampled"))
    if paired is None:
        pair_list = cf.get("lead_vs_live")
        if isinstance(pair_list, (list, tuple)) and pair_list:
            paired = (min(pair_list), max(pair_list))
    if isinstance(paired, (list, tuple)) and len(paired) == 2:
        paired_band = (min(paired[0], paired[1]), max(paired[0], paired[1]))
        inside = paired_band[0] <= 0 <= paired_band[1]
        # The band must also be a RANGE OF THIS LEAD, not of some other row:
        # a paired band that does not contain the published head-to-head lead
        # was computed against a different pair and must not vote. The
        # tolerance is a rounding epsilon — `median` is rounded to 2dp on both
        # sides while `per_coin` entries are rounded independently, so a lead
        # can sit 0.01 outside its own band by arithmetic alone.
        if not (paired_band[0] - 0.05 <= lead <= paired_band[1] + 0.05):
            inside = False
    else:
        inside = live_band[0] <= lead <= live_band[1]
    # (b) dropping one coin erases the lead
    drop_max = cf.get("loo_drop_max")
    worst = min((cf.get("per_coin") or {}).items(),
                key=lambda kv: kv[1]["ret"], default=(None, None))[0]
    erased = drop_max is not None and (lead - drop_max) <= 0
    # (c) the lead lives in one hot half-fold
    frac = _seg_lead_frac(chal.get("halves"), live_row.get("halves"))

    reasons = []
    if not incumbent:          # a row cannot be one-coin luckier than itself
        if inside:
            if paired_band is not None:
                # Same-universe form, so the sentence is not just the number.
                reasons.append(
                    f"paired lead {lead:+.2f}pp lies inside its own "
                    f"leave-one-coin-out band [{paired_band[0]:+.2f}, "
                    f"{paired_band[1]:+.2f}] — dropping a single coin flips "
                    f"which config wins")
            else:
                reasons.append(f"lead {lead:+.2f}pp sits inside the LIVE config's own "
                               f"leave-one-coin-out band [{live_band[0]:+.2f}, "
                               f"{live_band[1]:+.2f}]")
        if erased:
            reasons.append(f"dropping {worst} alone cuts the challenger's median "
                           f"{drop_max:.2f}pp -> lead {lead - drop_max:+.2f}pp")
        if frac is not None and frac < 0.75:
            reasons.append(f"only {frac:.0%} of 150d segments prefer it")
        # (d) THE SEGMENT TEST IS ONE-SIDED (2026-11-09). `frac` counts the
        # segments the challenger WINS; a negative median lead can still clear
        # 0.75 (measured: 0.88 at -18.73pp) because it wins small and loses
        # big. A negative lead cannot be "preferred" by the segment test,
        # whatever fraction it wins. Its own refusal, not a side effect.
        if frac is not None and lead <= 0:
            reasons.append(
                f"{frac:.0%} of segments prefer it, but its MEDIAN lead is "
                f"{lead:+.2f}pp — it wins small and loses big, which is not "
                f"a preference")
    # A SAMPLED paired band can refuse, never admit: inside it means the lead
    # is noise; outside it means only that 12 draws missed the noise. Handing
    # it the admitting power the raw test had would be the same conflation
    # of a seed with a population that the 2026-11-03 rewrite removed.
    if not incumbent and sampled and paired_band is not None and not inside:
        reasons.append(
            "the paired lead band is a SEEDED SAMPLE (not every universe), so "
            "it cannot admit a crown — only refuse one")
    fragile = bool(reasons)
    if fragile:
        reasons.append(f"challenger band [{chal_band[0]:+.2f}, "
                       f"{chal_band[1]:+.2f}] vs LIVE {live_row.get('median')}%")

    # Is the INCUMBENT itself outside its own noise? A live median below its
    # leave-one-coin-out floor, or above its ceiling, is a genuine regime change.
    live_fragile = not (live_band[0] <= live_row.get("median", 0) <= live_band[1])
    return {"fragile": fragile, "live_fragile": live_fragile, "lead": lead,
            "is_incumbent": incumbent, "live_band": live_band,
            "inside_live_band": inside, "lead_erased_by_one_coin": erased,
            # Which test made the "inside" verdict: the same-universe paired band, or
            # (rows from an older caller) the raw live-band form.
            "inside_basis": ("paired_leave_one_coin_out"
                             if paired_band is not None else "live_band"),
            "paired_lead_band": paired_band,
            "paired_untested": paired_band is None,
            "seg_lead_frac": frac, "reason": "; ".join(reasons),
            # Which band the live verdict rests on: exhausted population vs a
            # seeded sample. Flows into the DECAYED sentence and the findings
            # file so the owner can see the width of the evidence, not just
            # the interval.
            "live_basis": lf.get("basis") or f"{lf.get('draws', 0)} draws",
            "live_draws": lf.get("draws"),
            "basis": (f"{cf.get('draws', 0)} leave-one-coin-out universes; "
                      f"lead {lead:+.2f}pp; live band "
                      f"[{live_band[0]:+.2f}, {live_band[1]:+.2f}]")}


def _change_lines(prev, cur, best, live_row):
    """The Telegram/`changed` lines. Kept pure so the refusal is testable."""
    changed = []
    pb = max(prev["rows"], key=score)
    best_row = _row_like(best) if "cfg" not in (best or {}) else best
    # News is a change against the LAST RUN, not an absolute ranking. When the
    # best config has not moved, there is nothing to announce — and evaluating
    # the NEW BEST branch anyway made it contradict the decay branch below in
    # the same run (2026-10-24: "REFUSED NEW BEST" naming the config the decay
    # line then called the live incumbent, in one message). `>=` because a
    # challenger that merely TIES the previous best is also not news; the
    # refusal reason for it would be fabricated from a stale comparison.
    if score(best_row) > score(pb):
        # HARD RULE 3 gate: a "NEW BEST" must beat BTC buy&hold, not
        # just the other configs. Without this, a config that merely
        # lost less than BTC would be announced as an edge.
        ok_bench, bench_detail = admissible(
            best_row, cur.get("bench_btc_median"))
        print(f"  benchmark gate: {bench_detail}")
        if not ok_bench:
            print(f"  SUPPRESSED: best config does not beat BTC buy&hold "
                  f"— not announcing an edge")
        else:
            # A tie on (fold count, median) is not news, and what it reports is
            # an artefact of grid ORDER: max() takes the first, and `best` is
            # never the live row on a tie, so the refusal branch above would
            # name the identical numbers and this branch would crown LIVE as
            # its own challenger in the same message. Say there was a tie.
            tied = _tied_challengers(cur.get("rows") or [], best_row, live_row)
            if tied:
                names = ", ".join(str(r["cfg"]) for r in tied)
                changed.append(
                    f"NEW BEST config: {best_row['cfg']} "
                    f"(median {best_row['median']}%, "
                    f"{best_row['pos_folds']}/{best_row['n_folds']} folds) "
                    f"— {bench_detail}")
                changed.append(
                    f"TIE on (folds, median): {names} and LIVE "
                    f"{live_row['cfg']} are identical — the grid's order chose "
                    f"the winner, not the data. No config change is implied.")
                print(f"  TIE at the crown: {names} == LIVE")
            else:
                # A NEW BEST that is one coin's luck is not a new best. Refuse it,
                # and SAY SO — a silent refusal looks like nothing happened.
                v = lead_is_coin_fragile(best_row, live_row,
                                         n_folds=best_row.get("n_folds", 4))
                if v["fragile"]:
                    name = (best_row.get("cfg") or
                            f"median {best_row['median']}%")
                    changed.append(
                        f"REFUSED 'NEW BEST' {name} (median "
                        f"{best_row['median']}%, {best_row['pos_folds']}/"
                        f"{best_row['n_folds']} folds): the lead over LIVE is "
                        f"one-coin luck — {v['reason']}. Not an edge; "
                        f"the incumbent stands.")
                    print(f"  REFUSED NEW BEST: {v['reason']}")
                else:
                    changed.append(f"NEW BEST config: {best_row['cfg']} "
                                   f"(median {best_row['median']}%, "
                                   f"{best_row['pos_folds']}/"
                                   f"{best_row['n_folds']} folds) "
                                   f"— {bench_detail}")
    if live_row and pb and score(live_row) < score(pb):
        # A decay claim must clear the SAME fragility idea: the live config
        # being out-scored is only news if it fell OUT of its own noise band.
        v = lead_is_coin_fragile(best_row, live_row,
                                 n_folds=live_row.get("n_folds", 4))
        if v["live_fragile"]:
            # State WHY it is outside its band. Until 2026-10-20 this line said
            # "below its own leave-one-coin-out floor" unconditionally, but
            # lead_is_coin_fragile() flags BOTH tails — a LIVE median ABOVE the
            # ceiling is a regime change too, and on the 2026-10-20 cache that
            # is the case in force. The message must not invent a direction.
            lf = live_row["fragility"]
            med = live_row["median"]
            if med < lf["loo_min"]:
                why = f"below its own leave-one-coin-out floor {lf['loo_min']:+.2f}pp"
            elif med > lf["loo_max"]:
                why = (f"ABOVE its own leave-one-coin-out ceiling "
                       f"{lf['loo_max']:+.2f}pp — regime break, not decay")
            else:
                why = (f"outside its own band [{lf['loo_min']:+.2f}, "
                       f"{lf['loo_max']:+.2f}] but the direction is unclassified")
            # The band is a statement about a SAMPLE unless the payload says it
            # is exhaustive. Name the basis inline so a reader cannot mistake
            # 12 draws (8 universes) for the population — this sentence is what
            # the owner acts on, and on 2026-11-03 it was 8 of 23 universes.
            if lf.get("basis"):
                why += f" [{lf['basis']}]"
            changed.append(f"LIVE EDGE DECAYED: {live_row['cfg']} now "
                           f"median {med}% "
                           f"({live_row['pos_folds']}/{live_row['n_folds']}) "
                           f"— {why}")
        else:
            print("  SUPPRESSED: live config is inside its own noise band "
                  "— not a decay verdict")
    return changed


def score(r):
    """Prefer robustness (positive folds) then median."""
    return (r["pos_folds"], r["median"])


def admissible(r, bench_btc_median):
    """HARD RULE 3: does this config beat BTC buy&hold?

    A config can be 4/4 folds positive and still have no edge — it merely
    lost less than BTC. The loop ranks configs against EACH OTHER, never
    against the benchmark, so without this check a "NEW BEST" that fails
    the system's own rule 3 can still be announced. That is the defect this
    closes: no claim leaves this loop that hasn't out-returned buy&hold.

    Returns (ok, detail). Detail is a human verdict string for the report.
    """
    if bench_btc_median is None:
        return False, "benchmark unavailable — claim BLOCKED (fail-safe)"
    med = r.get("median")
    if med is None:
        return False, "no median — claim BLOCKED (fail-safe)"
    edge_pp = round(med - bench_btc_median, 2)
    ok = med >= bench_btc_median
    detail = (f"{'BEATS' if ok else 'LAGS'} BTC buy&hold by {edge_pp:+.2f}pp "
              f"(config {med}% vs BTC {bench_btc_median}%)")
    return ok, detail


def _tied_challengers(rows, best, live_row):
    """Rows that TIE the crown and are NOT the incumbent.

    `score()` is (pos_folds, median) — integer fold counts and a median
    rounded to 2dp, so exact ties are ordinary. `max()` returns the FIRST, and
    LIVE is second in the grid (`run()`), so a tie is always won by whichever
    challenger sorts first. That is a fact about grid ORDER, not about the
    configs: `_change_lines()` would refuse the tied challenger (its lead is
    0.00pp, inside the live band) and announce the identical live row as a
    "NEW BEST" in the same message.

    Returns [] when the crown is not an exact tie, so the normal path is
    untouched.
    """
    if not best or not live_row or best is live_row:
        return []
    if score(best) != score(live_row):
        return []
    # The crown and the incumbent are already known, so they cannot also be
    # the "second" row. Everything else that scores the same is a tie.
    return [r for r in rows
            if r is not best and r is not live_row
            and score(r) == score(best)]


def _fragility_line(v):
    """One sentence about the leave-one-coin-out verdict, for the findings file.

    A crowning of the incumbent has NO challenger, and saying "best vs LIVE
    +0.00pp; holds" reads as a passed test it never took. The band is the
    only measurement that exists on that row, so state it.
    """
    if not v:
        return "no live row"
    own = "NO" if v.get("live_fragile") else "yes"
    basis = f" [{v['live_basis']}]" if v.get("live_basis") else ""
    if v.get("is_incumbent"):
        band = v.get("live_band")
        b = (f"; band [{band[0]:+.2f}, {band[1]:+.2f}]{basis}" if band else "")
        return ("the grid crowns LIVE itself — no challenger to test; "
                f"live belongs to its own band: {own}{b}")
    verdict = ("FRAGILE — " + v.get("reason", "")) if v.get("fragile") else "holds"
    basis = v.get("live_basis") or v.get("basis")
    tail = f" [{basis}]" if basis else ""
    return (f"best vs LIVE {v.get('lead', 0):+.2f}pp; {verdict}; "
            f"live belongs to its own band: {own}{tail}")


def main():
    cur = run()
    prev = None
    if STATE.exists():
        try:
            prev = json.loads(STATE.read_text())
        except Exception:
            pass

    live_row = next((r for r in cur["rows"]
                     if all(r["cfg"].get(k) == v for k, v in LIVE.items())), None)
    best = max(cur["rows"], key=score)
    changed = []

    if prev:
        changed = _change_lines(prev, cur, best, live_row)

    # ---- MANDATORY OVERFIT GUARD ----
    # We over-claimed twice (hold=30, the A-grade gate). Both were caught by
    # luck. This makes it systematic: no claim leaves this loop unchecked.
    # 2026-10-19: this gate used to be fed `round(4*0.75)=3` positive folds for
    # the LIVE row (a guess that happened to be right) while the loser
    # `hold=30` was fed its real 2/4 and the narrative claimed it still
    # failed. It is now fed the live row's ACTUAL count.
    guard_ok, guard_txt = None, "guard unavailable"
    try:
        sys.path.insert(0, str(BASE / "scripts"))
        import autonomy as _aut
        n_set = (live_row["trades"] if live_row and "trades" in live_row
                 else (best.get("trades") or 0))
        n_fold = (live_row["n_folds"] if live_row and "n_folds" in live_row
                  else best.get("n_folds", 3))
        n_pos = (live_row["pos_folds"] if live_row and "pos_folds" in live_row
                 else 0)
        champ = (prev and max(prev["rows"], key=score).get("pos_folds"))
        feats = len(LIVE)
        guard_note = ""
        if champ is not None and n_pos < champ:
            # Relative consistency is the CHECK; when the live config is worse
            # than last run's challenger the check is vacuous — say so.
            guard_note = (f" (note: LIVE is {n_pos}/{n_fold} vs last run's best "
                          f"{champ}/{n_fold} — relative check is vacuous)")
        guard_ok = _aut.cmd_guard(n_set, feats, n_fold, n_pos,
                                  champ_pos=champ)
        guard_txt = (("GUARD PASS — claim admissible (live journal still the "
                      "final referee)" if guard_ok else
                      "GUARD FAIL — DO NOT CLAIM AN EDGE from this run")
                     + guard_note)
    except Exception as e:
        guard_txt = f"guard error: {str(e)[:70]}"

    lines = [f"# Research findings — {cur['ts'][:16]}",
             f"benchmarks: BTC median {cur['bench_btc_median']}%, "
             f"basket median {cur['bench_eql_median']}%", "",
             "| config | folds | median | +folds | trades | LOO band |",
             "|---|---|---|---|---|---|"]
    for r in sorted(cur["rows"], key=score, reverse=True):
        mark = " **LIVE**" if r is live_row else ""
        frag = r.get("fragility") or {}
        band = (f"[{frag['loo_min']:+.1f},{frag['loo_max']:+.1f}]"
                if frag.get("tested") else "untested")
        lines.append(f"| `{r['cfg']}`{mark} | {r['rets']} | {r['median']}% | "
                     f"{r['pos_folds']}/{r['n_folds']} | {r.get('trades', '?')} | "
                     f"{band} |")
    ok_bench, bench_detail = admissible(best, cur.get("bench_btc_median"))
    v = lead_is_coin_fragile(best, live_row) if live_row else {}
    lines += ["", f"**{guard_txt}**",
              "",
              f"**BENCHMARK (rule 3):** {bench_detail} — "
              f"{'ADMISSIBLE' if ok_bench else 'NOT AN EDGE'}",
              "",
              "**COIN FRAGILITY (leave-one-coin-out):** " + _fragility_line(v)]
    FINDINGS.parent.mkdir(parents=True, exist_ok=True)
    FINDINGS.write_text("\n".join(lines) + "\n")
    print(f"  {guard_txt}")

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(cur, indent=1))

    print(f"[{datetime.now(timezone.utc):%m-%d %H:%M}] research loop done | "
          f"best median {best['median']}% ({best['pos_folds']}/"
          f"{best['n_folds']} folds) | live median "
          f"{live_row['median'] if live_row else '?'}%")
    if changed:
        for c in changed:
            print(f"  CHANGE: {c}")
        try:
            sys.path.insert(0, str(BASE / "src"))
            from trading.tg.bot_ui import send_card
            body = "\n".join(changed) + f"\n\nbench BTC {cur['bench_btc_median']}%" \
                   f" | basket {cur['bench_eql_median']}%"
            send_card("signals", "🔬 RESEARCH LOOP — change detected", body)
        except Exception:
            pass
    else:
        print("  no change (silence by design)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"research loop error: {str(e)[:200]}")
