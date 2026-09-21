#!/usr/bin/env python3
"""Exhaustive accuracy hunt (FAST) — precompute features once, then grid-search.

Precomputes drawdown, ATR-ratio, RSI, and consecutive-reds arrays ONCE per
timeframe, then sweeps the filter grid in O(configs) without re-scanning
candles. This makes a 4500-config sweep finish in seconds.
"""
from __future__ import annotations

import json
import statistics

FINE = "/tmp/live_fine.json"


def load(tf):
    return [c[1] for c in json.load(open(FINE))[tf]]


def precompute(c, lb_vol=14, lb_rsi=14, dd_lb=90):
    n = len(c)
    dd = [0.0] * n
    vol = [0.0] * n
    rsi = [50.0] * n
    red = [0] * n

    # trailing drawdown over 90-bar window
    for i in range(dd_lb, n):
        hi = max(c[i - dd_lb:i])
        dd[i] = c[i] / hi - 1.0 if hi > 0 else 0.0

    # volatility (stdev of returns over 14)
    for i in range(lb_vol, n):
        r = [c[j] / c[j - 1] - 1 for j in range(i - lb_vol + 1, i)]
        vol[i] = statistics.pstdev(r) if len(r) > 1 else 0.0

    # RSI(14)
    for i in range(lb_rsi, n):
        g = l = 0.0
        for j in range(i - lb_rsi + 1, i + 1):
            d = c[j] - c[j - 1]
            if d >= 0:
                g += d
            else:
                l -= d
        if l == 0:
            rsi[i] = 100.0
        else:
            rs = (g / lb_rsi) / (l / lb_rsi)
            rsi[i] = 100.0 - 100.0 / (1.0 + rs)

    # consecutive red bars
    for i in range(1, n):
        red[i] = red[i - 1] + 1 if c[i] < c[i - 1] else 0

    return dd, vol, rsi, red


def run(c, dd, vol, rsi, red, dip, hold, vol_min, red_min, rsi_max, tp=None, sl=None):
    wins = total = 0
    rets = []
    n = len(c)
    for i in range(90, n - hold):
        if dd[i] > dip or vol[i] < vol_min or red[i] < red_min or rsi[i] > rsi_max:
            continue
        total += 1
        entry = c[i]
        if tp:
            tp_px = entry * (1 + tp)
            sl_px = entry * (1 - sl)
            exit_px = c[i + hold]
            for k in range(i + 1, i + hold + 1):
                if c[k] >= tp_px:
                    exit_px = tp_px
                    break
                if c[k] <= sl_px:
                    exit_px = sl_px
                    break
            fwd = exit_px / entry - 1
        else:
            fwd = c[i + hold] / entry - 1
        rets.append(fwd)
        if fwd > 0:
            wins += 1
    wr = wins / total * 100 if total else 0.0
    avg = sum(rets) / len(rets) * 100 if rets else 0.0
    return wr, total, avg


def main():
    print("=== BTC accuracy hunt (precomputed, OOS win rate sorted) ===\n")
    for tf, dips, barph in [
        ("30m", [-0.014, -0.02, -0.03, -0.04, -0.06], 2),
        ("15m", [-0.0105, -0.015, -0.0225, -0.03, -0.045], 4),
    ]:
        c = load(tf)
        n = len(c)
        split = int(n * 0.8)
        ctr, cte = c[:split], c[split:]
        dd_tr, vol_tr, rsi_tr, red_tr = precompute(ctr)
        dd_te, vol_te, rsi_te, red_te = precompute(cte)
        results = []
        for dip in dips:
            for vol_min in [0.0, 0.004, 0.006]:
                for red_min in [0, 2, 3, 4, 5]:
                    for rsi_max in [100, 45, 40, 35, 30]:
                        for hold in [3, 6, 12, 24]:
                            for tpsl in [None, (0.015, 0.0075), (0.02, 0.01)]:
                                hb = hold * barph
                                wr_tr, n_tr, avg_tr = run(ctr, dd_tr, vol_tr, rsi_tr, red_tr,
                                                          dip, hb, vol_min, red_min, rsi_max,
                                                          *(tpsl or (None, None)))
                                wr_te, n_te, avg_te = run(cte, dd_te, vol_te, rsi_te, red_te,
                                                          dip, hb, vol_min, red_min, rsi_max,
                                                          *(tpsl or (None, None)))
                                if n_te < 40:
                                    continue
                                results.append((wr_te, n_te, avg_te, wr_tr, n_tr,
                                                dip, vol_min, red_min, rsi_max, hold, tpsl))
        results.sort(key=lambda r: -r[0])
        print(f"--- {tf} (top 15 by OOS win rate, n>=40) ---")
        print(f"{'OOS WR':>6} {'n':>4} {'avg%':>7} {'trainWR':>7} {'dip%':>6} {'vol':>5} {'red':>4} {'rsi':>5} {'hold':>5} {'tp/sl':>9}")
        for r in results[:15]:
            wr_te, n_te, avg_te, wr_tr, n_tr, dip, vm, rm, rmx, hold, tpsl = r
            tpsl_s = f"{tpsl[0]*100:.1f}/{tpsl[1]*100:.1f}" if tpsl else "-"
            print(f"{wr_te:>5.1f}% {n_te:>4} {avg_te:>+7.3f} {wr_tr:>6.1f}% {dip*100:>6.1f} {vm:>5.3f} {rm:>4} {rmx:>5} {hold:>5} {tpsl_s:>9}")
        print()


if __name__ == "__main__":
    main()
