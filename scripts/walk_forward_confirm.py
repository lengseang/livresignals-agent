#!/usr/bin/env python3
"""Walk-forward validation of the capitulation-catch RSI<=30 config.

Instead of one 80/20 split (which can flatter or punish a lucky regime),
walk-forward rolls the train/test window forward across the data and reports
the distribution of OOS win rates. A robust edge shows consistent win rates
across EVERY window, not one lucky slice.

Config under test: dip + high-vol + RSI<=30 (+ optional red bars), hold 6 bars.
"""
from __future__ import annotations

import json
import statistics

FINE = "/tmp/live_fine.json"  # BTC 15m/30m (3.6yr)
ALTS = "/tmp/alts_fine.json"  # ETH/SOL (added separately)


def load(tf, sym="BTC/USDT"):
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


def run(c, dd, vol, rsi, red, dip, hold, vol_min, red_min, rsi_max):
    wins = total = 0
    rets = []
    for i in range(90, len(c) - hold):
        if dd[i] > dip or vol[i] < vol_min or red[i] < red_min or rsi[i] > rsi_max:
            continue
        total += 1
        fwd = c[i + hold] / c[i] - 1
        rets.append(fwd)
        if fwd > 0:
            wins += 1
    wr = wins / total * 100 if total else 0.0
    avg = sum(rets) / len(rets) * 100 if rets else 0.0
    return wr, total, avg


def walk_forward(c, dip, hold, vol_min, red_min, rsi_max, windows=6):
    """Rolling train/test. Returns list of (train_wr, test_wr, n_test)."""
    dd, vol, rsi, red = precompute(c)
    n = len(c)
    results = []
    step = n // (windows + 1)
    for w in range(windows):
        split = step * (w + 1)
        train_end = split
        test_end = min(split + step, n)
        if test_end - split < 200:
            continue
        wr_tr, n_tr, avg_tr = run(c[:train_end], dd[:train_end], vol[:train_end],
                                  rsi[:train_end], red[:train_end], dip, hold, vol_min, red_min, rsi_max)
        wr_te, n_te, avg_te = run(c[train_end:test_end], dd[train_end:test_end],
                                  vol[train_end:test_end], rsi[train_end:test_end],
                                  red[train_end:test_end], dip, hold, vol_min, red_min, rsi_max)
        results.append((wr_tr, n_tr, wr_te, n_te, avg_te))
    return results


def main():
    configs = [
        ("15m BTC: dip-4.5% vol.006 rsi30", "15m", "BTC/USDT", -0.045, 24, 0.006, 0, 30),
        ("15m BTC: dip-2.2% vol.006 rsi30", "15m", "BTC/USDT", -0.022, 24, 0.006, 0, 30),
        ("30m BTC: dip-4% vol.006 red2 rsi35", "30m", "BTC/USDT", -0.04, 12, 0.006, 2, 35),
    ]
    print("=== WALK-FORWARD (rolling windows) ===\n")
    for name, tf, sym, dip, hold, vm, rm, rmx in configs:
        c = load(tf, sym)
        res = walk_forward(c, dip, hold, vm, rm, rmx)
        test_wrs = [r[2] for r in res if r[3] >= 20]
        if not test_wrs:
            print(f"{name}: insufficient data")
            continue
        avg_wr = sum(test_wrs) / len(test_wrs)
        lo = min(test_wrs)
        hi = max(test_wrs)
        print(f"{name}")
        print(f"  windows={len(test_wrs)}  OOS WR: mean={avg_wr:.1f}%  min={lo:.1f}%  max={hi:.1f}%")
        for r in res:
            if r[3] >= 20:
                print(f"    train={r[0]:.1f}% (n={r[1]:>4})  ->  test={r[2]:.1f}% (n={r[3]:>4}, avg {r[4]:+.3f}%)")
        print()


if __name__ == "__main__":
    main()
