#!/usr/bin/env python3
"""Full sweep — ALL timeframes × ALL symbols × big/small filters.

Consumes data/full_sweep.json (ts+close for 30 symbols × 5 timeframes).
Sweeps the capitulation-catch variant space and ranks by profit factor on
an out-of-sample split. This is the "test everything" engine.

The dip threshold scales with timeframe (5m needs smaller dips than 4h).
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

DATA = Path("/opt/trading-agent/data/full_sweep.json")


def precompute(c):
    n = len(c)
    dd = [0.0] * n
    vol = [0.0] * n
    rsi = [50.0] * n
    red = [0] * n
    for i in range(90, n):
        hi = max(c[i - 90:i])
        dd[i] = c[i] / hi - 1.0 if hi > 0 else 0.0
    for i in range(14, n):
        r = [c[j] / c[j - 1] - 1 for j in range(i - 13, i)]
        vol[i] = statistics.pstdev(r) if len(r) > 1 else 0.0
    for i in range(14, n):
        g = l = 0.0
        for j in range(i - 13, i + 1):
            d = c[j] - c[j - 1]
            if d >= 0:
                g += d
            else:
                l -= d
        if l == 0:
            rsi[i] = 100.0
        else:
            rs = (g / 14) / (l / 14)
            rsi[i] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(1, n):
        red[i] = red[i - 1] + 1 if c[i] < c[i - 1] else 0
    return dd, vol, rsi, red


def evaluate(c, dd, vol, rsi, red, dip, hold, vm, rm, rmx):
    wins = losses = 0
    wret, lret = [], []
    for i in range(90, len(c) - hold):
        if dd[i] > dip or vol[i] < vm or red[i] < rm or rsi[i] > rmx:
            continue
        fwd = c[i + hold] / c[i] - 1
        if fwd > 0:
            wins += 1
            wret.append(fwd)
        else:
            losses += 1
            lret.append(fwd)
    total = wins + losses
    if total < 15:
        return None
    wr = wins / total * 100
    ev = (sum(wret) + sum(lret)) / total * 100
    gp = sum(wret) * 100
    gl = abs(sum(lret)) * 100
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0)
    return {"wr": wr, "n": total, "ev": ev, "pf": pf}


def main():
    d = json.load(open(DATA))
    # dip thresholds scale by timeframe (bigger candles -> bigger dips)
    dip_scale = {
        "5m": [-0.005, -0.01, -0.02, -0.03],
        "15m": [-0.01, -0.02, -0.04, -0.06],
        "30m": [-0.015, -0.03, -0.05, -0.08],
        "1h": [-0.02, -0.04, -0.06, -0.10],
        "4h": [-0.03, -0.06, -0.10, -0.15],
    }
    # holds: number of bars (per timeframe, ~1-2x the bar length)
    holds = {
        "5m": [12, 24, 48],
        "15m": [12, 24, 48],
        "30m": [6, 12, 24],
        "1h": [3, 6, 12],
        "4h": [2, 3, 6],
    }
    vm_grid = [0.0, 0.004]
    rm_grid = [0, 3, 4]
    rmx_grid = [100, 40, 30]

    results = []
    total_variants = 0
    for key, series in d.items():
        tf, sym = key.split(":", 1)
        c = [x[1] for x in series]
        if len(c) < 500:
            continue
        dd, vol, rsi, red = precompute(c)
        n = len(c)
        split = int(n * 0.7)
        for dip in dip_scale[tf]:
            for vm in vm_grid:
                for rm in rm_grid:
                    for rmx in rmx_grid:
                        for hold in holds[tf]:
                            total_variants += 1
                            r = evaluate(c[split:], dd[split:], vol[split:],
                                         rsi[split:], red[split:], dip, hold, vm, rm, rmx)
                            if r is None:
                                continue
                            results.append({
                                "symbol": sym, "tf": tf, "dip": dip, "vol": vm,
                                "red": rm, "rsi": rmx, "hold": hold,
                                "wr": round(r["wr"], 1), "n": r["n"],
                                "ev": round(r["ev"], 3), "pf": round(r["pf"], 2),
                            })

    results.sort(key=lambda r: -r["pf"])
    out = Path("/opt/trading-agent/reports/full-sweep-leaderboard.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(f"=== {total_variants} variants, {len(results)} with n>=15, ranked by PF (OOS) ===\n")
    print(f"{'symbol':<12} {'tf':<4} {'dip%':>6} {'vol':>5} {'red':>4} {'rsi':>4} {'hold':>5} {'WR%':>6} {'n':>4} {'EV%':>7} {'PF':>6}")
    print("-" * 72)
    shown = 0
    for r in results:
        if r["n"] < 25:
            continue
        print(f"{r['symbol']:<12} {r['tf']:<4} {r['dip']*100:>6.1f} {r['vol']:>5.3f} {r['red']:>4} {r['rsi']:>4} {r['hold']:>5} {r['wr']:>5.1f}% {r['n']:>4} {r['ev']:>+7.3f} {r['pf']:>6.2f}")
        shown += 1
        if shown >= 30:
            break


if __name__ == "__main__":
    main()
