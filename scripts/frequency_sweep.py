"""Frequency vs edge tradeoff — find a config that trades MORE without losing the edge.

The bulletproof validation revealed the real problem: 68.8% win rate but only
11-16 independent trades in 1.5 years. To make money, we need FREQUENCY.

This sweeps the filter thresholds to find the sweet spot where we get
(1) more trades AND (2) still positive expectancy AND (3) still positive IC.

Tested dimensions:
  - vol threshold: 0.006 / 0.008 / 0.010 / 0.012
  - red bars: 1 / 2 / 3
  - hold: 12h / 24h / 48h (shorter = more trades)
  - Also: adding ETH + SOL (multi-symbol frequency)
"""
from __future__ import annotations

import sqlite3
import statistics
from trading.config import SQLITE_PATH


def load_rows(symbol="BTC/USDT"):
    con = sqlite3.connect(str(SQLITE_PATH))
    con.row_factory = sqlite3.Row
    return con.execute(
        "SELECT close FROM candles WHERE symbol=? AND timeframe='1h' ORDER BY open_time_utc ASC",
        (symbol,)).fetchall()


def vol_at(rows, i, lookback=14):
    if i < lookback: return 0.0
    rets = [rows[j]['close']/rows[j-1]['close']-1 for j in range(i-lookback+1, i)]
    return statistics.pstdev(rets) if len(rets) > 1 else 0.0


def reds(rows, i, k):
    return sum(1 for j in range(max(1,i-k), i+1) if rows[j]['close'] < rows[j-1]['close'])


def run(rows, hold, dip_thresh, vol_min, red_min, embargo=True):
    wins=0; total=0; rets=[]; last=-10**9
    for i in range(90, len(rows)-hold-1):
        if embargo and i - last < hold: continue
        hi = max(r['close'] for r in rows[i-90:i])
        dd = rows[i]['close']/hi - 1
        if dd > dip_thresh: continue
        if vol_at(rows, i) < vol_min: continue
        if reds(rows, i, red_min) < red_min: continue
        entry = rows[i+1]['close']  # conservative: next close
        exit_px = rows[i+1+hold]['close']
        fwd = exit_px/entry - 1
        rets.append(fwd); total += 1
        if fwd > 0: wins += 1
        last = i
    wr = wins/total*100 if total else 0
    avg = sum(rets)/len(rets)*100 if rets else 0
    return wr, total, avg


if __name__ == "__main__":
    btc = load_rows("BTC/USDT")
    eth = load_rows("ETH/USDT")
    sol = load_rows("SOL/USDT")
    print("Sweep: frequency vs edge (BTC only, embargoed, enter next-close)")
    print(f"{'config':<34} {'WR%':>6} {'n':>4} {'avg%':>7}")
    configs = [
        ("vol .010 + 3red + 48h (current)", 48, -0.05, 0.010, 3),
        ("vol .010 + 2red + 48h", 48, -0.05, 0.010, 2),
        ("vol .008 + 2red + 48h", 48, -0.05, 0.008, 2),
        ("vol .008 + 2red + 24h", 24, -0.05, 0.008, 2),
        ("vol .006 + 2red + 24h", 24, -0.05, 0.006, 2),
        ("vol .006 + 1red + 24h", 24, -0.05, 0.006, 1),
        ("vol .008 + 1red + 12h", 12, -0.05, 0.008, 1),
        ("vol .006 + 1red + 12h", 12, -0.05, 0.006, 1),
    ]
    for name, hold, dt, vm, rm in configs:
        wr, n, avg = run(btc, hold, dt, vm, rm)
        print(f"{name:<34} {wr:>5.1f}% {n:>4} {avg:>+6.3f}")
    print()
    print("Multi-symbol frequency (vol .008 + 2red + 24h):")
    for sym, rows in [("BTC", btc), ("ETH", eth), ("SOL", sol)]:
        wr, n, avg = run(rows, 24, -0.05, 0.008, 2)
        print(f"  {sym}: WR {wr:.1f}% n={n} avg {avg:+.3f}%")
    total_n = sum(run(r, 24, -0.05, 0.008, 2)[1] for r in [btc, eth, sol])
    print(f"  COMBINED 3-symbol: ~{total_n} trades in 1.5yr (~{total_n/18:.1f}/month)")
