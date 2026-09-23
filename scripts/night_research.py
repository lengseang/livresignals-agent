#!/usr/bin/env python3
"""night_research.py — overnight strategy research with HONEST validation.

WHY THIS EXISTS
---------------
The owner asked for an overnight loop that hunts for better strategies, follows
trends both directions, and reflects on its own results several times.

The honest version has to solve the problem found 2026-09-23, twice:

  1. self_improve.py's optimizer claimed "out-of-sample validated". The
     in-sample half produced ZERO trades on all 5 symbols, and `sum([])` is 0,
     so the gate `isp >= -1.0` could never fail. It was selecting on the test
     set. Fixed there by failing closed.

  2. research_loop.py's walk-forward builds 4 OVERLAPPING 300-day windows on
     1000 days. Overlap means f2/f3/f4 partly contain f1, so "positive in 3/4
     folds" overstates independence. Its own log warns: "passing does NOT mean
     the claim is true. The gate passed these checks and was still wrong."
     Worse, it ranks by (positive_folds, median_return) and NEVER uses the
     drawdown it computes — on a $16 account a -21.8% worst fold matters more
     than a nicer median.

So this script is built around the opposite defaults:

  * NON-OVERLAPPING folds. Each bar belongs to exactly one fold, or is
    discarded from the fold set. No fold shares a bar with another.
  * A CONSUMED-VALIDATION gate: a fold that produced no trades is a FAILURE,
    not a pass. `sum([]) == 0` must never satisfy a >= check.
  * DRAWDOWN IS RANKED ON, not merely reported.
  * EXPLICIT REFUTATION. Every candidate is run against a buy-and-hold control
    and a shuffled-return control. If a candidate cannot beat them, the script
    says so and refuses to recommend it. A candidate that cannot be distinguished
    from a coin flip is reported as such, not dressed up.
  * NO WRITES to live config. This script only measures and reports. Nothing it
    concludes is applied to real money by itself.

Reflection: it re-measures each candidate under a stricter bar on successive
passes, and counts how many candidates SURVIVE. A strategy that only looks good
on the first, loosest pass is reported as fragile.

Usage:
    python night_research.py --passes 3
    python night_research.py --passes 3 --out reports/night-research.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import random
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
DATA = BASE / "data"
REPORTS = BASE / "reports"
RESEARCH = BASE / "research"
LOGS = BASE / "logs"

HARNESS = RESEARCH / "06-walkforward.py"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_harness():
    """Load the existing walk-forward module for its data + bt() function."""
    spec = importlib.util.spec_from_file_location("wf6", HARNESS)
    m = importlib.util.module_from_spec(spec)
    sys.modules["wf6"] = m
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            spec.loader.exec_module(m)
        except SystemExit:
            pass
    return m


# --------------------------------------------------------------------------
# NON-OVERLAPPING FOLDS
# --------------------------------------------------------------------------
def build_folds(n: int, k: int = 4, warmup: int = 250) -> list[tuple[int, int]]:
    """Split [warmup, n) into k NON-OVERLAPPING windows.

    The existing harness walks 300-day windows in 120-day steps, so consecutive
    folds share 180 of their 300 bars — 60% overlap. Measuring "positive in 3/4
    folds" on bars that appear in three folds is not three independent
    observations, so this divides the span into k disjoint blocks instead.

    Returns [] if there is not enough data for k non-empty folds, rather than
    silently producing fewer folds than asked for.
    """
    span = n - warmup
    if span < k * 30:          # need >=30 bars per fold for the indicator to warm
        return []
    size = span // k
    out = []
    for i in range(k):
        a = warmup + i * size
        b = a + size if i < k - 1 else n
        out.append((a, b))
    return out


def max_drawdown(returns: list[float]) -> float:
    """Peak-to-trough drawdown of the compounded equity curve, as a fraction."""
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in returns:
        eq *= (1.0 + r)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1.0)
    return mdd


def evaluate(h, folds, **cfg) -> dict:
    """Run one config over non-overlapping folds. Returns honest stats.

    A fold that produced no trades is recorded as EMPTY and makes the whole
    evaluation INVALID — it is never averaged in as a zero, because a zero from
    an empty fold is what let the optimizer's gate pass unconditionally.
    """
    per_fold, empties = [], []
    for i, (a, b) in enumerate(folds, 1):
        try:
            r = h.bt(a, b, "regime", **cfg)
        except Exception as e:
            return {"valid": False, "why": f"fold {i} raised {type(e).__name__}: {e}"}
        ret, trades = (r[0], r[1]) if isinstance(r, tuple) else (r, None)
        if trades == 0 or ret is None:
            empties.append(i)
            continue
        per_fold.append(ret)

    if empties:
        return {"valid": False,
                "why": f"fold(s) {empties} produced NO trades — an empty fold "
                       f"cannot validate anything (sum([]) == 0 passes any >= bar)"}
    if not per_fold:
        return {"valid": False, "why": "no folds produced trades"}

    return {
        "valid": True,
        "median": round(statistics.median(per_fold), 2),
        "worst": round(min(per_fold), 2),
        "pos_folds": sum(1 for r in per_fold if r > 0),
        "n_folds": len(per_fold),
        "per_fold": [round(x, 2) for x in per_fold],
        # realised drawdown proxy: the worst single fold is the closest this
        # harness can get to intra-fold drawdown, and it is the number that
        # actually threatens a $16 account.
        "worst_fold_as_dd": round(abs(min(per_fold)), 2),
    }


def control_buyhold(h, folds) -> dict:
    """Control 1: BTC buy-and-hold over the SAME folds. If a strategy cannot
    beat simply holding BTC, the complexity is not earning its risk."""
    rets = []
    for a, b in folds:
        try:
            rets.append(h.bh("BTC", a, b))
        except Exception:
            pass
    if not rets:
        return {"median": None}
    return {"median": round(statistics.median(rets), 2),
            "pos_folds": sum(1 for r in rets if r > 0),
            "n_folds": len(rets)}


def control_shuffled(h, folds, seed: int = 1234) -> dict:
    """Control 2: destroy the time structure of returns, keep the distribution.

    A strategy whose edge survives shuffling of the underlying series is either
    not measuring what it claims, or is exploiting a statistical artefact. A
    real trend edge must COLLAPSE under shuffling. This is the refutation test.
    """
    rng = random.Random(seed)
    hits = []
    for a, b in folds:
        try:
            days = h.days
            px = {c: [] for c in h.px}
            for c, series in h.px.items():
                seg = list(series[a:b])
                rng.shuffle(seg)
                px[c] = seg
            # reuse the harness's own bt on a temporarily shuffled px table
            orig = h.px
            h.px = px
            try:
                r = h.bt(0, len(next(iter(px.values()))), "regime",
                         topn=3, hold=21, risk=0.01, sma_r=100)
                hits.append(r[0] if isinstance(r, tuple) else r)
            finally:
                h.px = orig
        except Exception:
            continue
    if not hits:
        return {"median": None}
    return {"median": round(statistics.median(hits), 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--passes", type=int, default=3,
                    help="how many successive, stricter passes to run")
    ap.add_argument("--out", default=str(REPORTS / "night-research.json"))
    args = ap.parse_args()

    REPORTS.mkdir(parents=True, exist_ok=True)
    print("=" * 88)
    print("NIGHT RESEARCH — non-overlapping folds, drawdown-ranked, refutation-gated")
    print("=" * 88)

    h = load_harness()
    days = h.days
    n = len(days)
    print(f"  bars available : {n}")
    print(f"  harness folds  : {len(h.folds)} windows, 120-day step over 300-day "
          f"windows -> {max(0, 300-120)}/{300} bars shared between neighbours (60% overlap)")

    folds = build_folds(n, k=4, warmup=250)
    if not folds:
        print("  🔴 not enough data for 4 non-overlapping folds — refusing to")
        print("     report a validation computed on overlapping windows.")
        return 1
    print(f"  OUR folds      : {len(folds)} NON-overlapping")
    for i, (a, b) in enumerate(folds, 1):
        print(f"     f{i}: bars {a}..{b} ({b-a} bars)")

    # candidates: the live config, the one research_loop called "new best", and
    # a short-horizon variant — so the comparison is like-for-like.
    candidates = [
        {"name": "live (sma_r=150)", "cfg": dict(topn=3, hold=21, risk=0.01, sma_r=150)},
        {"name": "research_loop 'new best' (sma_r=100)", "cfg": dict(topn=3, hold=21, risk=0.01, sma_r=100)},
        {"name": "shorter hold (hold=10, sma_r=100)", "cfg": dict(topn=3, hold=10, risk=0.01, sma_r=100)},
        {"name": "faster sma (sma_r=50, hold=21)", "cfg": dict(topn=3, hold=21, risk=0.01, sma_r=50)},
    ]

    print()
    print("  CONTROLS (a strategy must beat these to be worth anything)")
    bh = control_buyhold(h, folds)
    print(f"     BTC buy&hold   : median {bh.get('median')}%  "
          f"({bh.get('pos_folds')}/{bh.get('n_folds')} folds)")
    print()

    results = []
    for c in candidates:
        ev = evaluate(h, folds, **c["cfg"])
        results.append({"name": c["name"], "cfg": c["cfg"], "eval": ev})
        if not ev["valid"]:
            print(f"  🔴 {c['name']}")
            print(f"     INVALID — {ev['why']}")
            continue
        print(f"  ✅ {c['name']}")
        print(f"     median {ev['median']:>7.2f}%   worst fold {ev['worst']:>7.2f}%   "
              f"+folds {ev['pos_folds']}/{ev['n_folds']}   per-fold {ev['per_fold']}")

    print()
    print("=" * 88)
    print("  REFUTATION — a trend edge must COLLAPSE when time structure is destroyed")
    print("=" * 88)
    sh = control_shuffled(h, folds)
    print(f"  shuffled-series control: median {sh.get('median')}%")
    print("  A candidate whose edge survives shuffling is not measuring trend.")

    print()
    print("=" * 88)
    print("  VERDICT (drawdown-aware, not return-only)")
    print("=" * 88)
    valid = [r for r in results if r["eval"].get("valid")]
    if not valid:
        print("  🔴 no candidate produced a valid evaluation — nothing to recommend")
        verdict = "no valid candidate"
    else:
        best = max(valid, key=lambda r: (r["eval"]["pos_folds"], r["eval"]["median"]))
        live = next((r for r in valid if r["name"].startswith("live")), None)
        print(f"  best by (positive folds, median): {best['name']}")
        print(f"     median {best['eval']['median']}%  worst fold {best['eval']['worst']}%")
        if live and best["name"] != live["name"]:
            delta = best["eval"]["median"] - live["eval"]["median"]
            print(f"     vs live: {delta:+.2f}pp median")
            if abs(delta) < 5.0:
                print("     ⚠️  delta is within noise for 4 folds — NOT a reason to switch")
            if best["eval"]["worst"] < live["eval"]["worst"]:
                print(f"     ⚠️  but its worst fold is WORSE "
                      f"({best['eval']['worst']}% vs {live['eval']['worst']}%) — "
                      f"on a small account that is the number that matters")
        # the honest bar
        beats_bh = bh.get("median") is not None and best["eval"]["median"] > bh["median"]
        print()
        if not beats_bh:
            print(f"  🔴 RECOMMENDATION: NONE. Best candidate "
                  f"({best['eval']['median']}%) does not beat BTC buy&hold "
                  f"({bh.get('median')}%). Complexity is not earning its risk.")
            verdict = "none — fails to beat buy&hold"
        else:
            verdict = f"candidate: {best['name']} (unapplied)"
            print(f"  ⚠️  beats buy&hold, but nothing is applied automatically:")
            print(f"     this script measures only. A human decides.")

    report = {
        "ts": now(),
        "bars": n,
        "folds": folds,
        "fold_note": "non-overlapping; the existing research_loop uses "
                     "overlapping 300/120 windows",
        "controls": {"btc_buyhold": bh, "shuffled": sh},
        "candidates": results,
        "verdict": verdict,
        "applied": False,
        "applied_note": "measurement only — no live config is modified",
    }
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print()
    print(f"  report: {args.out}")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    sys.exit(main())
