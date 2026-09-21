#!/usr/bin/env python3
"""STEP 4 (prove it first): Does regime separation actually improve win rate?

The core hypothesis of the adaptive framework: a fixed strategy only wins in
certain market states. If TRUE, then splitting trades by regime should show a
clear, stable win-rate difference between regimes — one regime with strong
positive expectancy, another with weak/negative.

If regimes DON'T separate win rates meaningfully, the adaptive framework is
built on a false premise and we shouldn't waste time on it.

Test: run the capitulation-catch strategy on 30m BTC, but tag each entry with
its regime (trending vs ranging, and high-vol vs low-vol), then compare win
rate + expectancy across regimes, walk-forward.

Regime definitions (simple, robust):
  - TRENDING: 90-bar linear slope / price > 0.05% per bar
  - RANGING:  otherwise
  - HIGH_VOL: 14-bar stdev > median
  - LOW_VOL:  otherwise
"""
from __future__ import annotations

import json
import statistics

FINE = "/tmp/live_fine.json"


def load():
    return [c[1] for c in json.load(open(FINE))["30m"]]


def precompute(c):
    n = len(c)
    dd = [0.0] * n
    vol = [0.0] * n
    rsi = [50.0] * n
    red = [0] * n
    slope = [0.0] * n
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
    # 90-bar normalized slope
    for i in range(90, n):
        seg = c[i - 90:i]
        m = len(seg)
        x = list(range(m))
        xm = (m - 1) / 2
        ym = sum(seg) / m
        denom = sum((xi - xm) ** 2 for xi in x) or 1
        num = sum((x[j] - xm) * (seg[j] - ym) for j in range(m))
        slope[i] = (num / denom) / (ym or 1)
    return dd, vol, rsi, red, slope


def evaluate(c, dd, vol, rsi, red, slope, dip, hold, vm, rm, rmx):
    """Returns dict keyed by regime label with (wr, n, ev)."""
    buckets = {}
    for i in range(90, len(c) - hold):
        if dd[i] > dip or vol[i] < vm or red[i] < rm or rsi[i] > rmx:
            continue
        # regime tag
        trend = "TREND" if slope[i] > 0.0005 else ("DOWNTREND" if slope[i] < -0.0005 else "RANGE")
        voltag = "HIVOL" if vol[i] > 0.004 else "LOVOL"
        key = f"{trend}+{voltag}"
        fwd = c[i + hold] / c[i] - 1
        b = buckets.setdefault(key, {"wins": 0, "losses": 0, "rets": []})
        if fwd > 0:
            b["wins"] += 1
        else:
            b["losses"] += 1
        b["rets"].append(fwd)
    out = {}
    for k, b in buckets.items():
        total = b["wins"] + b["losses"]
        wr = b["wins"] / total * 100 if total else 0.0
        ev = sum(b["rets"]) / total * 100 if total else 0.0
        out[k] = (wr, total, ev)
    return out


def main():
    c = load()
    dd, vol, rsi, red, slope = precompute(c)
    n = len(c)
    print(f"30m BTC: {n} candles ({n/24/365:.1f} years)\n")
    print("=== REGIME SEPARATION TEST (capitulation-catch, dip-2% highvol 3red rsi30, hold 6h) ===\n")
    print(f"{'regime':<18} {'win rate':>9} {'n':>5} {'EV%/trade':>10}")
    print("-" * 46)
    # full-sample first (larger n for clear signal)
    full = evaluate(c, dd, vol, rsi, red, slope, -0.02, 12, 0.004, 3, 30)
    for k in sorted(full, key=lambda x: -full[x][2]):
        wr, total, ev = full[k]
        print(f"{k:<18} {wr:>8.1f}% {total:>5} {ev:>+9.3f}")

    # walk-forward stability check: split into 3 and re-evaluate
    print("\n=== WALK-FORWARD (3 windows) to check stability ===\n")
    for w in range(3):
        s = n // 3 * w
        e = n // 3 * (w + 1)
        sub = evaluate(c[s:e], dd[s:e], vol[s:e], rsi[s:e], red[s:e], slope[s:e],
                       -0.02, 12, 0.004, 3, 30)
        print(f"window {w + 1} ({n//3} candles):")
        for k in sorted(sub, key=lambda x: -sub[x][2]):
            wr, total, ev = sub[k]
            print(f"    {k:<16} {wr:>6.1f}% (n={total}) EV {ev:+.3f}%")
        print()


if __name__ == "__main__":
    main()
