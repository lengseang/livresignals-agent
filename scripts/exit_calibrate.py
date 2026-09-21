#!/usr/bin/env python3
"""Exit calibration — the self-measuring loop (the exit's analogue of Layer 4).

WHY THIS EXISTS
---------------
Measured 2026-09-19: the live book held a median 4.0h while the peak arrived
around 2-3h, giving back ~100% of the peak move. Exiting at 2h turned
-0.36%/trade into +0.27%/trade and win rate 36.7% -> 60.0%.

But a hard-coded 2h would be exactly the kind of stale constant this system keeps
getting bitten by. And time-to-peak is NOT stable: across volatility buckets it
runs 2.24h (high vol) to 4.47h (mid vol) — a 2.23h spread — and it splits on BTC
slope too (2.22h flat vs 3.74h rising).

Hand-fitting a volatility table was TESTED and did not beat the fixed clock
(see references/exit-timing-and-payoff.md). The phenomenon is real; fitting it
with ~10 trades per bucket is not.

So: do not fit. MEASURE. This script re-derives the expected hold from our own
closed trades on a schedule and publishes it, the same shape as
weekly_regime_refit.py does for regime edges.

ROBUSTNESS RULES (a self-calibrating loop must not itself be a source of
instability — every one of these exists because the alternative overfits):
  1. MIN_N      - refuse to publish below a sample floor; keep the default.
  2. MEDIAN     - use the MEDIAN of per-trade optimal exits, not the argmax of
                  the aggregate curve. An argmax over 14 candidates on a small
                  sample is a noise picker; a median is not.
  3. PLATEAU    - the chosen hold must have positive-expectancy neighbours on
                  both sides. A needle-shaped optimum is rejected.
  4. CLAMP      - hold stays inside [MIN_HOLD, MAX_HOLD] no matter what.
  5. MAX_STEP   - never move more than this per re-fit, so one odd week cannot
                  whipsaw the exit.
  6. UN-CENSORED - price paths are read out to exit + EXTEND_H. Measuring the
                  peak only inside the trade's own window is CENSORING, and it
                  produced a falsely early 1.72h figure before this rule existed.

Output: data/exit-params.json
  {"hold_hours": 2.0, "disaster_stop_pct": 0.03, "n": int, "candidates": {...},
   "method": "...", "as_of_utc": "..."}
Consumers should fall back to DEFAULT_HOLD when this file is missing.
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "src"))
sys.path.insert(0, str(BASE / "scripts"))

TRADES = BASE / "data" / "live-trades.json"
OUT = BASE / "data" / "exit-params.json"

DEFAULT_HOLD = 2.0
MIN_HOLD, MAX_HOLD = 0.5, 6.0
MIN_N = 25                    # raised 20 -> 25 (stricter: 30 trades is already thin)
MAX_STEP = 0.25               # halved 0.5 -> 0.25: smaller moves per re-fit
MIN_IMPROVEMENT = 0.0005      # 0.05pp — do NOT move unless CLEARLY better
SPLIT_TOL = 1                 # split-half halves must agree within this many steps
EXTEND_H = 8
FEE = 0.001
DISASTER_STOP = 0.03          # wide crash insurance; a tight stop MEASURABLY hurt
CANDIDATES = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0,
              2.25, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0]


def parse(ts):
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def load_closed():
    try:
        return json.loads(TRADES.read_text(encoding="utf-8")).get("closed", [])
    except Exception:
        return []


def build_paths(ex):
    """Every closed trade with an UN-CENSORED hourly path."""
    out = []
    for t in load_closed():
        o, c = parse(t.get("open_ts_utc")), parse(t.get("close_ts_utc"))
        if not (t.get("symbol") and t.get("entry") and t.get("exit") and o and c):
            continue
        try:
            k = ex.publicGetKlines({
                "symbol": t["symbol"].replace("/", ""), "interval": "15m",
                "startTime": int(o.timestamp() * 1000),
                "endTime": int((c + timedelta(hours=EXTEND_H)).timestamp() * 1000),
                "limit": 1000})
            bars = [(int(r[0]), float(r[2]), float(r[3]), float(r[4])) for r in k]
        except Exception:
            bars = []
        if bars:
            out.append({"symbol": t["symbol"], "entry": float(t["entry"]),
                        "open": o, "bars": bars})
    return out


def ret_at(trade, hours):
    entry, dl = trade["entry"], trade["open"] + timedelta(hours=hours)
    px = next((c for ms, _, _, c in trade["bars"]
               if datetime.fromtimestamp(ms / 1000, timezone.utc) >= dl),
              trade["bars"][-1][3])
    return px / entry - 1 - 2 * FEE


def calibrate(paths, current_hold):
    if len(paths) < MIN_N:
        return None, f"only {len(paths)} trades (< {MIN_N}) — keep default", {}

    curve = {}
    for h in CANDIDATES:
        rets = [ret_at(t, h) for t in paths]
        curve[h] = {"avg": statistics.mean(rets), "n": len(rets),
                    "wr": sum(1 for r in rets if r > 0) / len(rets)}

    # 2. MEDIAN of each trade's OWN best exit — robust to a single outlier
    per_trade_best = []
    for t in paths:
        best = max(CANDIDATES, key=lambda h: ret_at(t, h))
        per_trade_best.append(best)
    median_hold = statistics.median(per_trade_best)

    # snap to the nearest candidate
    hold = min(CANDIDATES, key=lambda h: abs(h - median_hold))

    # 3. PLATEAU — never publish an optimum whose immediate neighbours are not
    #    ALSO positive. A needle-shaped peak is a noise pick, not a level: the
    #    kline sample here is small and an argmax over 14 candidates on 30 trades
    #    will happily select coincidence.
    #    IMPORTANT: reject only that candidate — do NOT silently revert to the
    #    old default, or the loop stops being a measurement and becomes a constant
    #    that merely re-asserts itself. Take the best candidate that passes.
    def neighbours(h):
        i = CANDIDATES.index(h)
        return (CANDIDATES[max(0, i - 1)], CANDIDATES[min(len(CANDIDATES) - 1, i + 1)])

    def plateau_ok(h):
        lo, hi = neighbours(h)
        return (curve[h]["avg"] > 0 and curve[lo]["avg"] > 0 and curve[hi]["avg"] > 0)

    valid = [h for h in CANDIDATES if plateau_ok(h)]
    if valid:
        hold = max(valid, key=lambda h: curve[h]["avg"])
        lo, hi = neighbours(hold)
        note = (f"median per-trade optimum {median_hold:.2f}h; "
                f"best plateau-valid hold {hold}h "
                f"(avg {curve[hold]['avg']*100:+.2f}%, neighbours "
                f"{lo}h={curve[lo]['avg']*100:+.2f}% / "
                f"{hi}h={curve[hi]['avg']*100:+.2f}%)")
        if hold != min(CANDIDATES, key=lambda h: abs(h - median_hold)):
            note += (f" | plateau rejected the raw median "
                     f"{min(CANDIDATES, key=lambda h: abs(h - median_hold))}h")
    else:
        hold = min(CANDIDATES, key=lambda h: abs(h - DEFAULT_HOLD))
        note = ("no candidate had a positive plateau — market behaviour is not "
                "currently exploitable at any tested hold; fell back to default")

    # 6. SPLIT-HALF AGREEMENT — the strictest gate. Split the trades in half by
    #    time and require both halves to independently prefer the same region.
    #    A hold that only wins in one half is a regime artifact, not a
    #    calibration, and must not be adopted.
    half = len(paths) // 2

    def best_valid(sub):
        if len(sub) < 8:
            return None
        cur = {h: statistics.mean([ret_at(t, h) for t in sub]) for h in CANDIDATES}
        ok2 = []
        for h in CANDIDATES:
            i = CANDIDATES.index(h)
            lo = CANDIDATES[max(0, i - 1)]
            hi = CANDIDATES[min(len(CANDIDATES) - 1, i + 1)]
            if cur[h] > 0 and cur[lo] > 0 and cur[hi] > 0:
                ok2.append(h)
        return max(ok2, key=lambda h: cur[h]) if ok2 else None

    h1, h2 = best_valid(paths[:half]), best_valid(paths[half:])
    if h1 is None or h2 is None:
        hold = min(CANDIDATES, key=lambda h: abs(h - DEFAULT_HOLD))
        note = (f"split-half gate: could not validate both halves "
                f"(h1={h1}, h2={h2}) — kept default {DEFAULT_HOLD}h")
    elif abs(CANDIDATES.index(h1) - CANDIDATES.index(h2)) > SPLIT_TOL:
        hold = current_hold
        note = (f"split-half DISAGREE: first half wants {h1}h, second wants {h2}h "
                f"(>{SPLIT_TOL} step apart) — held {current_hold}h, NO CHANGE")
    else:
        hold = max([h1, h2], key=lambda h: curve[h]["avg"])
        note += f" | split-half agree: h1={h1}h h2={h2}h"

    # 7. MIN_IMPROVEMENT — do not move for a marginal gain. Churn costs the
    #    track record more than a 0.05pp edge is worth.
    if hold != current_hold:
        gain = curve[hold]["avg"] - curve.get(current_hold, curve[hold])["avg"]
        if gain < MIN_IMPROVEMENT:
            note += (f" | gain {gain*100:+.3f}pp < {MIN_IMPROVEMENT*100:.2f}pp "
                     f"threshold — held {current_hold}h")
            hold = current_hold

    # 4. clamp   5. max step
    hold = max(MIN_HOLD, min(MAX_HOLD, hold))
    if abs(hold - current_hold) > MAX_STEP:
        hold = current_hold + MAX_STEP * (1 if hold > current_hold else -1)
        note += f" | clamped to a {MAX_STEP}h step from {current_hold}h"
        hold = min(CANDIDATES, key=lambda h: abs(h - hold))

    return hold, note, curve


# --------------------------------------------------------------------------
# STOP WIDTH — added 2026-09-19 after discovering this is the BIGGER leak.
#
# The live design stops at -1.00%, but the median deepest intra-trade dip across
# our own 30 trades is -1.31%. The stop therefore sits INSIDE the market's normal
# noise band and fires on ordinary wiggle: 12 of 30 trades exited on SL, versus 2
# on TP. Measured cost of that mistake, hold fixed at 2h:
#
#     stop -1.00% (current) : -0.05%/trade, payoff 0.87
#     stop -2.50%           : +0.26%/trade, payoff 1.60
#     stop -3.00%           : +0.30%/trade, payoff 1.73
#     stop -5.00%           : +0.45%/trade, payoff 2.26
#     no stop               : +0.23%/trade, payoff 1.34
#
# Monotonic until ~-5%, then no-stop is WORSE than a wide stop (a wide stop still
# cuts a genuine collapse; no stop does not). Widening does not merely raise the
# average — it IMPROVES the worst case too (-2.00% -> -1.78%), because fewer
# recoverable dips are converted into realised losses.
#
# Rule: never set the stop tighter than the typical dip, or it is a noise
# detector rather than a risk limit.
# --------------------------------------------------------------------------
STOP_CANDIDATES = [0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05]
MIN_STOP, MAX_STOP = 0.01, 0.08
DEFAULT_STOP = 0.03


def typical_dip(paths, hours, stop):
    """Median deepest dip inside the holding window — the noise floor."""
    dips = []
    for t in paths:
        dl = t["open"] + timedelta(hours=hours)
        lows = [low for ms, _, low, _ in t["bars"]
                if datetime.fromtimestamp(ms / 1000, timezone.utc) <= dl]
        if lows:
            dips.append(min(lows) / t["entry"] - 1)
    return statistics.median(dips) if dips else None


def sim_stop(paths, hold_h, stop):
    rets = []
    for t in paths:
        entry = t["entry"]
        sl = entry * (1 - stop)
        dl = t["open"] + timedelta(hours=hold_h)
        px = None
        for ms, high, low, close in t["bars"]:
            ts = datetime.fromtimestamp(ms / 1000, timezone.utc)
            if low <= sl:
                px = sl
                break
            if ts >= dl:
                px = close
                break
        if px is None:
            px = t["bars"][-1][3]
        rets.append(px / entry - 1 - 2 * FEE)
    return rets


def calibrate_stop(paths, hold_h, current_stop):
    dip = typical_dip(paths, hold_h, current_stop)
    curve = {}
    for s in STOP_CANDIDATES:
        r = sim_stop(paths, hold_h, s)
        curve[s] = {"avg": statistics.mean(r),
                    "wr": sum(1 for x in r if x > 0) / len(r),
                    "worst": min(r)}

    # the noise floor is a hard lower bound: a stop tighter than the typical dip
    # is not a risk limit, it is a dip detector.
    floor = abs(dip) * 1.5 if dip else MIN_STOP
    valid = [s for s in STOP_CANDIDATES
             if s >= max(MIN_STOP, min(MAX_STOP, floor))]

    def nb(s):
        i = STOP_CANDIDATES.index(s)
        return (STOP_CANDIDATES[max(0, i - 1)],
                STOP_CANDIDATES[min(len(STOP_CANDIDATES) - 1, i + 1)])

    def plateau(s):
        lo, hi = nb(s)
        return (curve[s]["avg"] > 0 and curve[lo]["avg"] > 0
                and curve[hi]["avg"] > 0)

    ok = [s for s in valid if plateau(s)]
    if not ok:
        return current_stop, (f"no stop width had a positive plateau "
                              f"(typical dip {dip*100:.2f}%) — kept {current_stop*100:.2f}%"), curve, dip

    # Selection rule: take the TIGHTEST stop that still reaches the plateau.
    # Once widening stops buying anything, extra width is pure unearned risk.
    # (An earlier version picked the candidate nearest the noise floor, which
    #  perversely selected the TIGHTEST stop — a bug that published 1.00% when
    #  the curve clearly plateaued from 2.50%. Selection must follow the MEASURED
    #  curve, not a fixed notion of "wider is better".)
    best_avg = max(curve[s]["avg"] for s in ok)
    tol = 0.0002                       # within 0.02pp counts as "plateau reached"
    reached = [s for s in ok if curve[s]["avg"] >= best_avg - tol]
    stop = min(reached)
    note = (f"typical intra-trade dip {dip*100:.2f}% (noise floor, "
            f"stop may not sit inside it); tightest stop reaching the plateau "
            f"{stop*100:.2f}% (avg {curve[stop]['avg']*100:+.2f}%, "
            f"worst {curve[stop]['worst']*100:+.2f}%, "
            f"best {best_avg*100:+.2f}%)")
    if current_stop < max(MIN_STOP, abs(dip) if dip else MIN_STOP):
        note += (f" | old stop {current_stop*100:.2f}% sat INSIDE the typical dip "
                 f"— it was a dip detector, not a risk limit")
    return stop, note, curve, dip


def main():
    from trading.exchange.live_client import make_live_exchange

    prev = {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
    current = float(prev.get("hold_hours", DEFAULT_HOLD))

    paths = build_paths(make_live_exchange())
    hold, note, curve = calibrate(paths, current)
    if hold is None:
        hold = current

    # STOP WIDTH — only meaningful on the same sample floor
    prev_stop = float(prev.get("disaster_stop_pct", DEFAULT_STOP))
    if len(paths) >= MIN_N:
        stop, stop_note, stop_curve, dip = calibrate_stop(paths, hold, prev_stop)
    else:
        stop, stop_note, stop_curve, dip = prev_stop, "insufficient sample", {}, None

    doc = {
        # SCOPE GUARD — this calibration belongs to ONE book. Measured
        # 2026-09-21: the dip-buy book peaks at a median 2h, while trend_follower
        # peaks at a median 73.8h (3 days) with only 22% giveback. Applying this
        # hold to trend_follower would cut every position before it peaks and
        # destroy the book. Do not consume this file outside the scope below.
        "scope": "live-trades.json (dip-buy spot executor) ONLY",
        "not_applicable_to": ["trend_follower", "lev_satellite",
                              "regime_trader", "adaptive_engine"],
        "hold_hours": round(hold, 2),
        "disaster_stop_pct": round(stop, 4),
        "typical_dip_pct": round(dip, 5) if dip else None,
        "n": len(paths),
        "samples": {str(h): {"avg": round(v["avg"], 5), "wr": round(v["wr"], 3),
                             "n": v["n"]} for h, v in curve.items()},
        "stop_samples": {f"{s*100:.2f}%": {"avg": round(v["avg"], 5),
                                           "wr": round(v["wr"], 3),
                                           "worst": round(v["worst"], 5)}
                         for s, v in stop_curve.items()},
        "method": "median of per-trade optimal exit, plateau-checked, "
                  "clamped, max-step-limited; stop floored at 1.5x the typical "
                  "dip. Measured on un-censored paths.",
        "note": note,
        "stop_note": stop_note,
        "previous_hold_hours": current,
        "previous_stop_pct": prev_stop,
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    print(f"trades measured : {len(paths)}")
    print(f"hold            : {current}h -> {hold}h")
    print(f"  {note}")
    print(f"stop            : {prev_stop*100:.2f}% -> {stop*100:.2f}%")
    print(f"  {stop_note}")
    print()
    print(f"{'hold':>7}{'avg/tr':>11}{'win%':>8}{'n':>5}")
    for h, v in curve.items():
        print(f"{h:>6}h{v['avg']*100:>10.2f}%{v['wr']*100:>7.1f}%{v['n']:>5}")
    if stop_curve:
        print()
        print(f"{'stop':>8}{'avg/tr':>11}{'win%':>8}{'worst':>10}")
        for s, v in stop_curve.items():
            print(f"{s*100:>7.2f}%{v['avg']*100:>10.2f}%{v['wr']*100:>7.1f}%"
                  f"{v['worst']*100:>9.2f}%")
    print()
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
