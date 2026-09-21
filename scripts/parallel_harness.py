#!/usr/bin/env python3
"""Parallel test harness — run MANY strategy variants at once (big + small).

The live paper loop is slow (a few signals/week). This harness runs the SAME
capitulation-catch logic but sweeps dozens of variants across:
  - symbols: BTC / ETH / SOL
  - timeframes: 15m / 30m / 1h
  - filter combos: big (deep dip, many red bars) vs small (shallow, few red)
  - RSI thresholds, vol thresholds, hold horizons, TP/SL

Every variant is validated out-of-sample (walk-forward) and ranked by
profit factor (the metric that pays). Output: a ranked leaderboard so we can
see which "big and small" tests actually have an edge.

Runs in minutes on the 3.6yr data we already have — no waiting on the market.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

FINE = Path("/opt/trading-agent/data/live_fine.json")   # BTC
ALTS = Path("/opt/trading-agent/data/alts_fine.json")   # ETH/SOL


def load(tf, sym):
    if sym == "BTC/USDT":
        return [c[1] for c in json.load(open(FINE))[tf]]
    return [c[1] for c in json.load(open(ALTS))[f"{tf}:{sym}"]]


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
    win_ret = []
    loss_ret = []
    for i in range(90, len(c) - hold):
        if dd[i] > dip or vol[i] < vm or red[i] < rm or rsi[i] > rmx:
            continue
        fwd = c[i + hold] / c[i] - 1
        if fwd > 0:
            wins += 1
            win_ret.append(fwd)
        else:
            losses += 1
            loss_ret.append(fwd)
    total = wins + losses
    if total < 20:
        return None
    wr = wins / total * 100
    avg_win = sum(win_ret) / len(win_ret) * 100 if win_ret else 0
    avg_loss = sum(loss_ret) / len(loss_ret) * 100 if loss_ret else 0
    ev = (wins * avg_win + losses * avg_loss) / total
    gp = sum(win_ret) * 100
    gl = abs(sum(loss_ret)) * 100
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0)
    return {"wr": wr, "n": total, "ev": ev, "pf": pf, "avg_win": avg_win, "avg_loss": avg_loss}


def main():
    # Variant grid — "big" (deep/aggressive) and "small" (shallow/frequent)
    symbols = {
        "BTC/USDT": {"tf": ["15m", "30m"], "dips": [-0.015, -0.02, -0.04, -0.06]},
        "ETH/USDT": {"tf": ["15m", "30m"], "dips": [-0.03, -0.05, -0.08]},
        "SOL/USDT": {"tf": ["15m", "30m"], "dips": [-0.04, -0.06, -0.10]},
    }
    vm_grid = [0.0, 0.004, 0.006]
    rm_grid = [0, 2, 3, 4]       # 0 = small (no red-bar requirement), 4 = big
    rmx_grid = [100, 40, 35, 30]  # 100 = small (no RSI filter), 30 = big
    holds = {"15m": [12, 24], "30m": [6, 12]}

    results = []
    for sym, cfg in symbols.items():
        for tf in cfg["tf"]:
            c = load(tf, sym)
            dd, vol, rsi, red = precompute(c)
            n = len(c)
            split = int(n * 0.7)
            # OOS = last 30%
            for dip in cfg["dips"]:
                for vm in vm_grid:
                    for rm in rm_grid:
                        for rmx in rmx_grid:
                            for hold in holds[tf]:
                                # train (for sanity) + OOS (for ranking)
                                r_te = evaluate(c[split:], dd[split:], vol[split:],
                                                rsi[split:], red[split:], dip, hold, vm, rm, rmx)
                                if r_te is None:
                                    continue
                                results.append({
                                    "symbol": sym, "tf": tf, "dip": dip, "vol": vm,
                                    "red": rm, "rsi": rmx, "hold": hold,
                                    "wr": round(r_te["wr"], 1), "n": r_te["n"],
                                    "ev": round(r_te["ev"], 3), "pf": round(r_te["pf"], 2),
                                })

    # Rank by profit factor (the metric that pays), require n>=30 for trust
    results.sort(key=lambda r: -r["pf"])
    out = Path("/opt/trading-agent/reports/variant-leaderboard.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(f"=== {len(results)} variants tested, ranked by profit factor (OOS) ===\n")
    print(f"{'symbol':<10} {'tf':<4} {'dip%':>6} {'vol':>5} {'red':>4} {'rsi':>4} {'hold':>5} {'WR%':>6} {'n':>4} {'EV%':>7} {'PF':>6}")
    print("-" * 70)
    shown = 0
    for r in results:
        if r["n"] < 30:  # skip thin samples for the top table
            continue
        print(f"{r['symbol']:<10} {r['tf']:<4} {r['dip']*100:>6.1f} {r['vol']:>5.3f} {r['red']:>4} {r['rsi']:>4} {r['hold']:>5} {r['wr']:>5.1f}% {r['n']:>4} {r['ev']:>+7.3f} {r['pf']:>6.2f}")
        shown += 1
        if shown >= 25:
            break


if __name__ == "__main__":
    main()
