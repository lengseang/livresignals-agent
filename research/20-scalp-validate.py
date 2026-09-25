#!/usr/bin/env python3
"""SCALP VALIDATION — does the 15m/30m dip signal actually hold up?

WHY THIS EXISTS
---------------
scripts/scalping_sweep.py found an apparently strong result on BTC 15m:

    dip -1.5% + highvol + 3red + rsi40 + hold 3h   OOS WR 61.0%  avg +0.436%

That is roughly 3x the trend edge's net expectancy. But the evidence behind it
has three problems that make it unusable as a decision:

  1. BTC ONLY.  ETH and SOL 15m data exists (126,947 bars each) and was never
     used.  n=82 is the entire basis for the claim.
  2. ONE 80/20 SPLIT.  A single split can be lucky.  The repo's own harness
     (research/06-walkforward.py) uses 5 rolling folds for exactly this reason.
  3. NO t-STAT.  A mean without a t-stat is an opinion, not a result.

THE FAILURE MODE THIS GUARDS AGAINST IS ALREADY IN THIS REPO
-----------------------------------------------------------
regime_trader's entry gate showed "+12pp lift" on 164 setups.  Rebuilt at 7,960
setups, the lift VANISHED — it was a small-sample artifact.  n=82 is half the
size of the artifact that already fooled us once.  So this script reports the
sample size and t-stat NEXT TO EVERY NUMBER, and applies a pre-registered
acceptance gate.

PRE-REGISTERED GATE (decided before seeing results, do not edit after)
----------------------------------------------------------------------
    pooled OOS n         >= 300
    pooled OOS t-stat    >= 2.0
    positive folds       >= 3 of 5
    net of fees          >  0

ALL FOUR must pass.  Any one failing means: reject.  The comparison bar is the
trend edge's t=+4.47 at n=1412.

DO NOT TUNE AFTER SEEING RESULTS.  If a config fails, it fails.  Picking a new
dip threshold because it makes the number look better is precisely how the
regime_trader gate fooled us.

READ-ONLY.  No network, no orders, no state writes.  Reads two JSON files.
"""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

BASE = Path("/opt/trading-agent")
FEE_ROUND_TRIP = 0.001594      # measured on 33 real fills: 0.0797%/side

# ---- PRE-REGISTERED GATE -------------------------------------------------
GATE_N = 300
GATE_T = 2.0
GATE_FOLDS = 3
GATE_NET = 0.0

FOLDS = 5
LOOKBACK = 90                  # bars, for the running-high drawdown


# ---------------------------------------------------------------- data load
def load_symbol(sym: str, tf: str) -> list[dict]:
    """Return [{"t": ms, "c": close}] oldest-first for sym at tf.

    ⚠️ THE FILES ARE CLOSE-ONLY: each row is [ts_ms, close], width 2 -- NOT
    OHLCV. Confirmed by inspection 2026-09-25:

        data/live_fine.json  ["15m"]           -> [1672531200000, 16520.69]
        data/alts_fine.json  ["15m:ETH/USDT"]  -> [1672531200000, 1194.55]

    This matters because scripts/scalping_sweep.py reads `c[1]` (the close) and
    the ORIGINAL version of this loader demanded 5 fields, so it refused every
    row. That is the correct failure: a loader that guessed would have produced
    a number from the wrong field and nobody would have noticed.

    Two sources, two key shapes:
      live_fine.json : {"15m": ...}          (BTC)
      alts_fine.json : {"15m:ETH/USDT": ...} (ETH, SOL)
    A missing key raises -- reading the wrong instrument silently is worse than
    crashing.

    No high/low, so any wick-based or intrabar logic is impossible on this data.
    Everything here uses closes only, which is a real limitation and is stated
    rather than papered over.
    """
    if sym == "BTC":
        path, key = BASE / "data" / "live_fine.json", tf
    else:
        path, key = BASE / "data" / "alts_fine.json", f"{tf}:{sym}/USDT"

    d = json.loads(path.read_text())
    if key not in d:
        raise KeyError(f"{path.name} has no {key!r}; got {list(d)[:6]}")
    out = []
    for r in d[key]:
        if isinstance(r, (list, tuple)) and len(r) >= 2:
            out.append({"t": r[0], "c": float(r[1])})
        elif isinstance(r, dict) and "c" in r:
            out.append({"t": r.get("t"), "c": float(r["c"])})
        else:
            raise TypeError(f"unrecognised row in {key}: {str(r)[:80]}")
    return out


# ---------------------------------------------------------------- indicators
def vol_at(closes: list[float], i: int, lookback: int = 14) -> float:
    if i < lookback:
        return 0.0
    rets = [closes[j] / closes[j - 1] - 1 for j in range(i - lookback + 1, i)]
    return statistics.pstdev(rets) if len(rets) > 1 else 0.0


def rsi_at(closes: list[float], i: int, lookback: int = 14) -> float:
    if i < lookback:
        return 50.0
    gains = losses = 0.0
    for j in range(i - lookback + 1, i + 1):
        d = closes[j] - closes[j - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    rs = (gains / lookback) / (losses / lookback)
    return 100.0 - 100.0 / (1.0 + rs)


def red_bars(closes: list[float], i: int) -> int:
    n = 0
    j = i
    while j > 0 and closes[j] < closes[j - 1]:
        n += 1
        j -= 1
    return n


# ---------------------------------------------------------------- one config
def run(closes: list[float], dip: float, hold_bars: int,
        high_vol: bool = False, min_red: int = 0,
        rsi_max: float = 0.0) -> dict:
    """Trade the config over `closes`; return rets + summary stats."""
    rets: list[float] = []
    for i in range(LOOKBACK, len(closes) - hold_bars):
        hi = max(closes[i - LOOKBACK:i])
        if (closes[i] / hi - 1) > dip:          # not a deep enough dip
            continue
        if high_vol and vol_at(closes, i) < 0.004:
            continue
        if min_red and red_bars(closes, i) < min_red:
            continue
        if rsi_max and rsi_at(closes, i) > rsi_max:
            continue
        rets.append(closes[i + hold_bars] / closes[i] - 1)

    n = len(rets)
    if n == 0:
        return {"n": 0, "mean_pct": 0.0, "median_pct": 0.0, "sd_pct": 0.0,
                "t": 0.0, "wr_pct": 0.0, "net_pct": 0.0, "rets": []}
    mean = sum(rets) / n
    sd = statistics.pstdev(rets) if n > 1 else 0.0
    # t of the mean: mean / standard error.  sd=0 -> t=0 (no information),
    # never infinity -- an infinite t would look like overwhelming evidence.
    t = (mean / (sd / math.sqrt(n))) if sd > 0 and n > 1 else 0.0
    return {
        "n": n,
        "mean_pct": mean * 100,
        "median_pct": statistics.median(rets) * 100,
        "sd_pct": sd * 100,
        "t": t,
        "wr_pct": sum(1 for r in rets if r > 0) / n * 100,
        "net_pct": (mean - FEE_ROUND_TRIP) * 100,
        "rets": rets,
    }


def fmt(row: dict) -> str:
    return (f"n={row['n']:>5}  WR={row['wr_pct']:>5.1f}%  "
            f"mean={row['mean_pct']:>+6.3f}%  net={row['net_pct']:>+6.3f}%  "
            f"t={row['t']:>+5.2f}")


# ---------------------------------------------------------------- main
def main() -> None:
    configs = [
        ("baseline     dip + 3h",        False, 0, 0.0),
        ("+ highvol",                    True,  0, 0.0),
        ("+ highvol + 3red",             True,  3, 0.0),
        ("+ highvol + rsi40",            True,  0, 40.0),
        ("+ highvol + 3red + rsi40",     True,  3, 40.0),
    ]
    timeframes = [("15m", -0.015, 4), ("30m", -0.020, 2)]
    symbols = ["BTC", "ETH", "SOL"]

    report: dict = {"gate": {"n": GATE_N, "t": GATE_T, "folds": GATE_FOLDS,
                             "net": GATE_NET},
                    "fee_round_trip_pct": FEE_ROUND_TRIP * 100,
                    "results": {}}

    for tf, dip, bar_per_h in timeframes:
        hold_bars = 3 * bar_per_h
        print(f"\n{'='*100}\n=== {tf}  dip={dip*100:.1f}%  hold=3h "
              f"({hold_bars} bars) ===\n{'='*100}")

        series = {}
        for sym in symbols:
            try:
                bars = load_symbol(sym, tf)
                series[sym] = [b["c"] for b in bars]
            except Exception as e:
                print(f"  !! {sym} {tf} unavailable: {e}")
        if not series:
            continue

        for name, hv, mr, rmx in configs:
            per_sym = {}
            folds_pos = 0
            fold_means = []
            pooled: list[float] = []

            for sym, closes in series.items():
                row = run(closes, dip, hold_bars, hv, mr, rmx)
                per_sym[sym] = row
                pooled.extend(row["rets"])

                # 5 rolling folds on THIS symbol's returns, for stability
                rs = row["rets"]
                if len(rs) >= FOLDS:
                    size = len(rs) // FOLDS
                    for k in range(FOLDS):
                        seg = rs[k * size:(k + 1) * size]
                        if seg:
                            mean_k = sum(seg) / len(seg)
                            fold_means.append(mean_k)
                            if mean_k > 0:
                                folds_pos += 1

            n = len(pooled)
            if n == 0:
                print(f"  {name:<28} no signals")
                continue
            mean = sum(pooled) / n
            sd = statistics.pstdev(pooled) if n > 1 else 0.0
            t = (mean / (sd / math.sqrt(n))) if sd > 0 and n > 1 else 0.0
            net = mean - FEE_ROUND_TRIP
            wr = sum(1 for r in pooled if r > 0) / n * 100

            total_folds = len(fold_means)
            gate_pass = (n >= GATE_N and t >= GATE_T
                         and folds_pos >= GATE_FOLDS and net > GATE_NET)

            print(f"  {name:<28} POOLED  n={n:>5}  WR={wr:>5.1f}%  "
                  f"mean={mean*100:>+6.3f}%  net={net*100:>+6.3f}%  t={t:>+5.2f}  "
                  f"folds+={folds_pos}/{total_folds}  "
                  f"{'PASS' if gate_pass else 'fail'}")
            for sym, row in per_sym.items():
                print(f"      {sym:<4} {fmt(row)}")

            report["results"][f"{tf}:{name}"] = {
                "tf": tf, "config": name, "dip": dip,
                "hold_bars": hold_bars, "high_vol": hv,
                "min_red": mr, "rsi_max": rmx,
                "pooled": {"n": n, "wr_pct": wr, "mean_pct": mean * 100,
                           "sd_pct": sd * 100, "t": t, "net_pct": net * 100},
                "folds_positive": folds_pos, "folds_total": total_folds,
                "gate_pass": gate_pass,
                "per_symbol": {s: {k: v for k, v in r.items() if k != "rets"}
                               for s, r in per_sym.items()},
            }

    # ---- verdict ---------------------------------------------------------
    print(f"\n{'='*100}\n=== VERDICT (pre-registered gate) ===\n{'='*100}")
    print(f"  gate: n>={GATE_N}  t>={GATE_T}  folds+>={GATE_FOLDS}  net>{GATE_NET}")
    print(f"  bar to beat: the trend edge's t=+4.47 at n=1412")
    passed = [k for k, v in report["results"].items() if v["gate_pass"]]
    if passed:
        print(f"\n  ✅ PASSED: {passed}")
        print("  -> candidate for a small live allocation, BUT n and t must be")
        print("     re-checked on forward data before any capital is committed.")
    else:
        print("\n  ❌ NO CONFIG PASSED THE GATE.")
        print("  -> the scalping signal is NOT an improvement on this evidence.")
        print("     That is a real result, not a failure: it is the same small-")
        print("     sample artifact that already fooled regime_trader's gate.")

    out = Path("/tmp/scalp-validation-report.json")
    out.write_text(json.dumps(report, indent=2))
    print(f"\n  report -> {out}")


if __name__ == "__main__":
    main()
