"""Replay the ensemble's OWN exit rule against candles with reachable geometry.

The 2026-10-17 proposal decided the ensemble defect on a replay whose baseline
reproduced the 5m book to 1.5% and was 2.4x off on 30m. The report here is the
same construction on a longer window, and it *does not* reproduce the baseline:
the staircase is ~$0.008/trade kinder than the book. So this tool reports ORDERING
only, and the module docstring says so on stdout.

⚠️ This is a STAIRCASE: every `hold`-th bar is an entry, no overlap. The real
runner enters on a momentum signal and can overlap. Do not quote the levels.

    venv/bin/python research/_ensemble_geometry_replay.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path("/opt/trading-agent/data/trading.db")
SYMBOL = "BTC/USDT"
COST = 0.0028          # round trip, per the stack convention
SIZE = 5.0
LANES = (("5m", 48), ("30m", 12))
GEOMS = ((0.03, 0.02, "current 3.0/2.0"),
         (0.012, 0.010, "fix 1.2/1.0"),
         (0.006, 0.005, "fix 0.6/0.5"))


def _bars(con, tf):
    return con.execute(
        "select open_time_utc, open, high, low, close from candles "
        "where symbol=? and timeframe=? order by open_time_utc",
        (SYMBOL, tf)).fetchall()


def replay(rows, hold, tp_pct, sl_pct):
    w = l = t = 0
    pnl = 0.0
    for i in range(0, len(rows) - hold - 1, hold):
        entry = rows[i][4]
        tp, sl = entry * (1 + tp_pct), entry * (1 - sl_pct)
        seg = rows[i + 1:i + 1 + hold]
        ex = None
        for b in seg:
            if b[3] <= sl:
                ex = sl
                break
            if b[2] >= tp:
                ex = tp
                break
        if ex is None:
            ex = seg[-1][4]
            t += 1
        move = (ex - entry) / entry
        pnl += move - COST
        if move > 0:
            w += 1
        elif move < 0:
            l += 1
    n = w + l + t
    return {"n": n, "w": w, "l": l, "t": t,
            "per_trade": SIZE * pnl / n if n else 0.0}


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    print("ensemble geometry replay — STAIRCASE, ORDERING ONLY\n")
    for tf, hold in LANES:
        rows = _bars(con, tf)
        if len(rows) < hold * 20:
            print(f"{tf}: insufficient candles ({len(rows)})")
            continue
        print(f"{tf} hold={hold}")
        for tp_pct, sl_pct, label in GEOMS:
            r = replay(rows, hold, tp_pct, sl_pct)
            print(f"  {label:<18} n={r['n']:<4} W{r['w']} L{r['l']} T{r['t']}"
                  f"   per-trade ${r['per_trade']:+.5f}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
