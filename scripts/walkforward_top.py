#!/usr/bin/env python3
"""Walk-forward confirm the top variant candidates (big + small tests).

The single-split leaderboard inflates profit factors. This rolls the window
forward to see which variants hold up across MULTIPLE time periods, not one
lucky slice. Reports mean/min/max OOS win rate per candidate.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

FINE = Path("/opt/trading-agent/data/live_fine.json")
ALTS = Path("/opt/trading-agent/data/alts_fine.json")


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


def run(c, dd, vol, rsi, red, dip, hold, vm, rm, rmx):
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
    return wr, total, ev


def walk_forward(c, dip, hold, vm, rm, rmx, windows=5):
    dd, vol, rsi, red = precompute(c)
    n = len(c)
    step = n // (windows + 1)
    out = []
    for w in range(windows):
        s = step * (w + 1)
        e = min(s + step, n)
        r = run(c[s:e], dd[s:e], vol[s:e], rsi[s:e], red[s:e], dip, hold, vm, rm, rmx)
        if r:
            out.append(r)
    return out


def main():
    # top candidates from the leaderboard (the "big" tests that dominated)
    cands = [
        ("ETH/USDT", "15m", -0.08, 24, 0.004, 4, 40),
        ("ETH/USDT", "15m", -0.08, 24, 0.000, 4, 40),
        ("SOL/USDT", "15m", -0.10, 12, 0.004, 4, 100),
        ("SOL/USDT", "15m", -0.10, 12, 0.000, 4, 40),
        ("BTC/USDT", "15m", -0.04, 24, 0.006, 3, 100),
        ("BTC/USDT", "30m", -0.06, 12, 0.004, 4, 30),
        ("BTC/USDT", "15m", -0.04, 24, 0.004, 4, 40),
        ("SOL/USDT", "15m", -0.10, 12, 0.004, 3, 40),
    ]
    print(f"{'candidate':<28} {'mean WR':>8} {'min':>6} {'max':>6} {'windows':>8}")
    print("-" * 60)
    for sym, tf, dip, hold, vm, rm, rmx in cands:
        c = load(tf, sym)
        res = walk_forward(c, dip, hold, vm, rm, rmx)
        if not res:
            print(f"{sym} {tf} dip{dip*100:.0f} r{rm} rsi{rmx}: insufficient")
            continue
        wrs = [r[0] for r in res]
        mean = sum(wrs) / len(wrs)
        print(f"{sym} {tf} dip{dip*100:.0f} r{rm} rsi{rmx:<3}  {mean:>7.1f}% {min(wrs):>5.1f}% {max(wrs):>5.1f}% {len(wrs):>8}")


if __name__ == "__main__":
    main()
