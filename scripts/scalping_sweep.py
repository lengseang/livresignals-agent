#!/usr/bin/env python3
"""Scalping-edge sweep on fine candles (15m/30m) with honest OOS validation.

Tests whether a REAL intraday scalping edge exists on BTC 15m/30m data:
  ENTRY: dip depth (scaled for fine TF), high-vol, N consecutive red bars, RSI
  EXIT:  hold horizon in bars (1h, 3h, 6h, 12h)

Splits chronologically 80/20, reports train vs OOS. The point is to find a
config that holds OUT of sample with a meaningful sample size.
"""
from __future__ import annotations

import json
import statistics


def load(tf: str) -> list[float]:
    return [c[1] for c in json.load(open("/tmp/live_fine.json"))[tf]]


def vol_at(closes, i, lookback=14):
    if i < lookback:
        return 0.0
    rets = [closes[j] / closes[j - 1] - 1 for j in range(i - lookback + 1, i)]
    return statistics.pstdev(rets) if len(rets) > 1 else 0.0


def rsi_at(closes, i, lookback=14):
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


def red_bars(closes, i):
    n = 0
    j = i
    while j > 0 and closes[j] < closes[j - 1]:
        n += 1
        j -= 1
    return n


def run(closes, dip, hold_bars, high_vol, min_red, rsi_max):
    wins = total = 0
    rets = []
    lookback = 90
    for i in range(lookback, len(closes) - hold_bars):
        hi = max(closes[i - lookback:i])
        dd = closes[i] / hi - 1
        if dd > dip:
            continue
        if high_vol and vol_at(closes, i) < 0.004:
            continue
        if min_red and red_bars(closes, i) < min_red:
            continue
        if rsi_max and rsi_at(closes, i) > rsi_max:
            continue
        total += 1
        fwd = closes[i + hold_bars] / closes[i] - 1
        rets.append(fwd)
        if fwd > 0:
            wins += 1
    wr = wins / total * 100 if total else 0.0
    avg = sum(rets) / len(rets) * 100 if rets else 0.0
    return wr, total, avg


def main():
    for tf, dip, bar_per_h in [("15m", -0.015, 4), ("30m", -0.02, 2)]:
        closes = load(tf)
        n = len(closes)
        split = int(n * 0.8)
        tr, te = closes[:split], closes[split:]
        print(f"=== {tf} BTC: {n} candles, train={len(tr)} test={len(te)} ===")
        configs = [
            (f"dip {dip*100:.1f}% + hold 1h", dip, 1 * bar_per_h, False, 0, 0),
            (f"dip {dip*100:.1f}% + hold 3h", dip, 3 * bar_per_h, False, 0, 0),
            (f"dip {dip*100:.1f}% + hold 6h", dip, 6 * bar_per_h, False, 0, 0),
            (f"dip {dip*100:.1f}% + hold 12h", dip, 12 * bar_per_h, False, 0, 0),
            (f"dip {dip*100:.1f}% + highvol + hold 3h", dip, 3 * bar_per_h, True, 0, 0),
            (f"dip {dip*100:.1f}% + highvol + 3red + 3h", dip, 3 * bar_per_h, True, 3, 0),
            (f"dip {dip*100:.1f}% + highvol + rsi40 + 3h", dip, 3 * bar_per_h, True, 0, 40),
            (f"dip {dip*100:.1f}% + 5red + hold 3h", dip, 3 * bar_per_h, False, 5, 0),
            (f"dip {dip*100:.1f}% + highvol + 3red + rsi40 + 3h", dip, 3 * bar_per_h, True, 3, 40),
        ]
        rows = []
        for name, d, h, hv, mr, rmx in configs:
            wr_tr, n_tr, avg_tr = run(tr, d, h, hv, mr, rmx)
            wr_te, n_te, avg_te = run(te, d, h, hv, mr, rmx)
            rows.append((name, wr_tr, n_tr, avg_tr, wr_te, n_te, avg_te))
        rows.sort(key=lambda r: -r[4])
        print(f"{'config':<44} {'train WR':>8} {'n':>6} {'avg':>7} | {'OOS WR':>7} {'n':>6} {'avg':>7}")
        print("-" * 95)
        for name, a, b, c, d, e, f in rows:
            print(f"{name:<44} {a:>7.1f}% {b:>6} {c:>+6.3f} | {d:>6.1f}% {e:>6} {f:>+6.3f}")
        print()


if __name__ == "__main__":
    main()
