#!/usr/bin/env python3
"""ETH/SOL capitulation edge + EXPECTANCY (not just win rate) analysis.

Win rate alone is misleading. The metric that pays is expectancy:
  EV = P(win) * avg_win - P(loss) * avg_loss

Tests the stable 30m capitulation config (dip + highvol + 3red + rsi40) on
BTC/ETH/SOL, reporting win rate AND expectancy AND profit factor, with
walk-forward stability. Also tests RSI30 variant on alts to see if the
"deeply oversold" edge is real on higher-volatility alts.
"""
from __future__ import annotations

import json
import statistics

FINE = "/tmp/live_fine.json"
ALTS = "/tmp/alts_fine.json"


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
    wr = wins / total * 100 if total else 0.0
    avg_win = sum(win_ret) / len(win_ret) * 100 if win_ret else 0.0
    avg_loss = sum(loss_ret) / len(loss_ret) * 100 if loss_ret else 0.0
    # expectancy in % per trade
    ev = (wins * avg_win + losses * avg_loss) / total if total else 0.0
    # profit factor
    gp = sum(win_ret) * 100
    gl = abs(sum(loss_ret)) * 100
    pf = gp / gl if gl > 0 else float("inf")
    return wr, total, avg_win, avg_loss, ev, pf


def main():
    # dip scaled per symbol: alts are more volatile, so bigger dip thresholds
    symbols = {
        "BTC/USDT": -0.02,
        "ETH/USDT": -0.03,
        "SOL/USDT": -0.04,
    }
    print("=== EXPECTANCY ANALYSIS (30m, hold 12 bars, highvol + 3red + rsi40) ===\n")
    print(f"{'symbol':<10} {'WR%':>6} {'n':>5} {'avgWin%':>8} {'avgLoss%':>8} {'EV%/trade':>10} {'ProfitF':>8}")
    print("-" * 60)
    for sym, dip in symbols.items():
        c = load("30m", sym)
        dd, vol, rsi, red = precompute(c)
        # walk-forward: evaluate on last 30% as OOS
        n = len(c)
        split = int(n * 0.7)
        wr, total, aw, al, ev, pf = evaluate(c[split:], dd[split:], vol[split:],
                                             rsi[split:], red[split:], dip, 12, 0.004, 3, 40)
        print(f"{sym:<10} {wr:>5.1f}% {total:>5} {aw:>+7.3f} {al:>+7.3f} {ev:>+9.3f} {pf:>7.2f}")

    print("\n=== RSI30 variant (deeper oversold) on alts, 30m hold 12 ===\n")
    print(f"{'symbol':<10} {'WR%':>6} {'n':>5} {'avgWin%':>8} {'avgLoss%':>8} {'EV%/trade':>10} {'ProfitF':>8}")
    print("-" * 60)
    for sym, dip in symbols.items():
        c = load("30m", sym)
        dd, vol, rsi, red = precompute(c)
        n = len(c)
        split = int(n * 0.7)
        wr, total, aw, al, ev, pf = evaluate(c[split:], dd[split:], vol[split:],
                                             rsi[split:], red[split:], dip, 12, 0.004, 3, 30)
        print(f"{sym:<10} {wr:>5.1f}% {total:>5} {aw:>+7.3f} {al:>+7.3f} {ev:>+9.3f} {pf:>7.2f}")


if __name__ == "__main__":
    main()
