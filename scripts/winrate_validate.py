"""Validate the high-vol + shallow-dip win-rate improvement OUT OF SAMPLE.

The in-sample sweep found "dip -5% + high vol → 67.5% WR". But high-vol
filters are prone to overfitting. This splits the 1h data chronologically
and tests the config on the held-out second half — the honest test.
"""
from __future__ import annotations

import sqlite3
import statistics
from trading.config import SQLITE_PATH


def load_closes():
    con = sqlite3.connect(str(SQLITE_PATH))
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT close FROM candles WHERE symbol='BTC/USDT' AND timeframe='1h' ORDER BY open_time_utc ASC").fetchall()
    return [r['close'] for r in rows]


def compute_vol(closes, i, lookback=14):
    if i < lookback:
        return 0.0
    rets = [closes[j]/closes[j-1]-1 for j in range(i-lookback+1, i)]
    return statistics.pstdev(rets) if len(rets) > 1 else 0.0


def run(closes, dip_thresh, hold, vol_mode, vol_thresh):
    wins = 0; total = 0; rets = []
    for i in range(90, len(closes)-hold):
        hi = max(closes[i-90:i])
        dd = closes[i]/hi - 1
        if dd > dip_thresh:
            continue
        vol = compute_vol(closes, i)
        if vol_mode == "high" and vol < vol_thresh:
            continue
        if vol_mode == "low" and vol > vol_thresh:
            continue
        total += 1
        fwd = closes[i+hold]/closes[i] - 1
        rets.append(fwd)
        if fwd > 0:
            wins += 1
    wr = wins/total*100 if total else 0
    avg = sum(rets)/len(rets)*100 if rets else 0
    return wr, total, avg


if __name__ == "__main__":
    closes = load_closes()
    n = len(closes)
    half = n // 2
    first = closes[:half]
    second = closes[half:]
    print(f"1h data: {n} candles. Split: first {len(first)}, second {len(second)}")
    print()
    print("=== OUT-OF-SAMPLE VALIDATION (train on first half, test on second) ===")
    print(f"{'config':<34} {'1st-half WR':>12} {'2nd-half WR':>12} {'avg%':>7}")
    configs = [
        ("baseline dip -5%", -0.05, None, 0),
        ("+ high vol (>0.008)", -0.05, "high", 0.008),
        ("+ high vol (>0.010)", -0.05, "high", 0.010),
        ("+ high vol (>0.012)", -0.05, "high", 0.012),
    ]
    for name, th, vm, vt in configs:
        wr1, n1, a1 = run(first, th, 48, vm, vt)
        wr2, n2, a2 = run(second, th, 48, vm, vt)
        print(f"{name:<34} {wr1:>10.1f}% {wr2:>10.1f}% {a2:>+6.3f} (n2={n2})")
