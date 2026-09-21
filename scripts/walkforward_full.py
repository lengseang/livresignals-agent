#!/usr/bin/env python3
"""Walk-forward confirm the top full-sweep candidates across time windows.

Candidates to confirm (from the 5616-variant sweep):
  BTC 5m  dip-0.5/1/2/3  r3 rsi30 hold24
  BTC 5m  dip-0.5/1/2/3  r3 rsi30 hold12
  XRP 30m dip-8 r3/r4 rsi30/40 hold12
  ETH 15m dip-6 r4 rsi40/30 hold24
Runs rolling train/test windows; reports mean/min/max WR + EV per candidate.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

DATA = Path("/opt/trading-agent/data/full_sweep.json")


def load(tf, sym):
    d = json.load(open(DATA))
    return [x[1] for x in d[f"{tf}:{sym}"]]


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
    if total < 10:
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
    cands = [
        ("BTC/USDT", "5m", -0.005, 24, 0.004, 3, 30),
        ("BTC/USDT", "5m", -0.01, 24, 0.004, 3, 30),
        ("BTC/USDT", "5m", -0.02, 24, 0.004, 3, 30),
        ("BTC/USDT", "5m", -0.005, 12, 0.004, 3, 30),
        ("XRP/USDT", "30m", -0.08, 12, 0.004, 3, 30),
        ("XRP/USDT", "30m", -0.08, 12, 0.004, 4, 30),
        ("XRP/USDT", "30m", -0.08, 12, 0.004, 3, 40),
        ("ETH/USDT", "15m", -0.06, 24, 0.004, 4, 40),
        ("ETH/USDT", "15m", -0.06, 24, 0.004, 4, 30),
    ]
    print(f"{'candidate':<34} {'mean WR':>8} {'min':>6} {'max':>6} {'mean EV':>8} {'n/w':>6}")
    print("-" * 72)
    for sym, tf, dip, hold, vm, rm, rmx in cands:
        c = load(tf, sym)
        res = walk_forward(c, dip, hold, vm, rm, rmx)
        if not res:
            print(f"{sym} {tf} dip{dip*100:.1f} r{rm} rsi{rmx} hold{hold}: insufficient")
            continue
        wrs = [r[0] for r in res]
        evs = [r[2] for r in res]
        ns = [r[1] for r in res]
        mean = sum(wrs) / len(wrs)
        mean_ev = sum(evs) / len(evs)
        mean_n = sum(ns) / len(ns)
        print(f"{sym} {tf} dip{dip*100:.1f} r{rm} rsi{rmx} h{hold}   "
              f"{mean:>7.1f}% {min(wrs):>5.1f}% {max(wrs):>5.1f}% "
              f"{mean_ev:>+7.3f} {mean_n:>5.1f}")


if __name__ == "__main__":
    main()
