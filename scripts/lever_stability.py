"""Stability check for the 'vol + 3 red bars' winner (78.7% OOS).

Tests on MULTIPLE chronological splits to confirm the edge is stable,
not an artifact of one particular train/test boundary.
"""
from __future__ import annotations

import sqlite3
import statistics
from trading.config import SQLITE_PATH


def load_rows():
    con = sqlite3.connect(str(SQLITE_PATH))
    con.row_factory = sqlite3.Row
    return con.execute(
        "SELECT close FROM candles WHERE symbol='BTC/USDT' AND timeframe='1h' ORDER BY open_time_utc ASC"
    ).fetchall()


def vol_at(rows, i, lookback=14):
    if i < lookback: return 0.0
    rets = [rows[j]['close']/rows[j-1]['close']-1 for j in range(i-lookback+1, i)]
    return statistics.pstdev(rets) if len(rets) > 1 else 0.0


def run(rows, hold=48, dip_thresh=-0.05, vol_min=0.010, red_min=3):
    wins=0; total=0; rets=[]
    for i in range(90, len(rows)-hold):
        hi = max(r['close'] for r in rows[i-90:i])
        dd = rows[i]['close']/hi - 1
        if dd > dip_thresh: continue
        if vol_at(rows, i) < vol_min: continue
        reds = sum(1 for j in range(max(1,i-red_min), i+1) if rows[j]['close'] < rows[j-1]['close'])
        if reds < red_min: continue
        total += 1
        fwd = rows[i+hold]['close']/rows[i]['close'] - 1
        rets.append(fwd)
        if fwd > 0: wins += 1
    wr = wins/total*100 if total else 0
    avg = sum(rets)/len(rets)*100 if rets else 0
    return wr, total, avg


if __name__ == "__main__":
    rows = load_rows()
    n = len(rows)
    print(f"1h data: {n} candles")
    print()
    print("=== STABILITY: 'vol + 3 red' across 3 different OOS splits ===")
    print(f"{'split (train/test)':<28} {'OOS WR':>8} {'avg%':>8} {'n':>5}")
    # 3 different split points: 60/40, 70/30, 50/50
    for frac in [0.5, 0.6, 0.7]:
        cut = int(n * frac)
        test = rows[cut:]
        wr, nt, avg = run(test)
        print(f"{int(frac*100)}/{int((1-frac)*100)}  ({len(test)} test)   {wr:>6.1f}% {avg:>+7.3f} {nt:>5}")
    # Also: walk-forward windows (3 equal thirds)
    print()
    print("=== WALK-FORWARD THIRDS ===")
    third = n // 3
    for k in range(3):
        seg = rows[k*third:(k+1)*third]
        wr, nt, avg = run(seg)
        print(f"  third {k+1}: WR {wr:.1f}% avg {avg:+.3f}% n={nt}")
