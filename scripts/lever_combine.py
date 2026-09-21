"""Test INTERACTIONS of the top levers + out-of-sample validation.

Top single levers found: >=3 red bars (83.3%), RSI<40 (77.7%), 18-24 UTC
(78.0%), vol>3 (75.8%). Now test combinations, then validate the winner
on a chronological held-out split (the honest anti-overfit test).
"""
from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime, timezone
from trading.config import SQLITE_PATH


def load_rows():
    con = sqlite3.connect(str(SQLITE_PATH))
    con.row_factory = sqlite3.Row
    return con.execute(
        "SELECT close, volume, open_time_utc FROM candles WHERE symbol='BTC/USDT' AND timeframe='1h' ORDER BY open_time_utc ASC"
    ).fetchall()


def vol_at(rows, i, lookback=14):
    if i < lookback: return 0.0
    rets = [rows[j]['close']/rows[j-1]['close']-1 for j in range(i-lookback+1, i)]
    return statistics.pstdev(rets) if len(rets) > 1 else 0.0


def rsi_at(rows, i, lookback=14):
    if i < lookback: return 50.0
    gains=0.0; losses=0.0
    for j in range(i-lookback+1, i+1):
        d = rows[j]['close'] - rows[j-1]['close']
        if d >= 0: gains += d
        else: losses -= d
    if losses == 0: return 100.0
    rs = (gains/lookback)/(losses/lookback)
    return 100.0 - 100.0/(1.0+rs)


def red_bars(rows, i, k):
    return sum(1 for j in range(max(1,i-k), i+1) if rows[j]['close'] < rows[j-1]['close'])


def run(rows, hold=48, dip_thresh=-0.05, vol_min=0.010, rsi_max=None, red_min=None,
        hour_filter=None, bb_exp=None):
    wins=0; total=0; rets=[]
    for i in range(90, len(rows)-hold):
        hi = max(r['close'] for r in rows[i-90:i])
        dd = rows[i]['close']/hi - 1
        if dd > dip_thresh: continue
        v = vol_at(rows, i)
        if v < vol_min: continue
        if rsi_max is not None and rsi_at(rows, i) > rsi_max: continue
        if red_min is not None and red_bars(rows, i, red_min) < red_min: continue
        if hour_filter is not None:
            h = datetime.fromtimestamp(rows[i]['open_time_utc']/1000, tz=timezone.utc).hour
            if h not in hour_filter: continue
        if bb_exp is not None:
            seg = [rows[j]['close'] for j in range(max(1,i-20), i)]
            if len(seg) > 1:
                bw = statistics.pstdev(seg)/statistics.mean(seg)
                if bw < bb_exp: continue
        total += 1
        fwd = rows[i+hold]['close']/rows[i]['close'] - 1
        rets.append(fwd)
        if fwd > 0: wins += 1
    wr = wins/total*100 if total else 0
    avg = sum(rets)/len(rets)*100 if rets else 0
    return wr, total, avg


CONFIGS = [
    ("base (vol only)", dict()),
    ("vol + RSI<40", dict(rsi_max=40)),
    ("vol + 3 red", dict(red_min=3)),
    ("vol + RSI<40 + 3 red", dict(rsi_max=40, red_min=3)),
    ("vol + RSI<40 + 18-24UTC", dict(rsi_max=40, hour_filter=set(range(18,24)))),
    ("vol + 3 red + RSI<40 + 18-24", dict(rsi_max=40, red_min=3, hour_filter=set(range(18,24)))),
]


if __name__ == "__main__":
    rows = load_rows()
    n = len(rows); half = n//2
    first, second = rows[:half], rows[half:]
    print(f"1h data: {n} candles. OOS split: {len(first)} / {len(second)}")
    print()
    print(f"{'config':<36} {'IN-sample WR':>13} {'OUT-sample WR':>14} {'OOS avg%':>8}")
    for name, kw in CONFIGS:
        wr_in, ni, _ = run(first, **kw)
        wr_out, no, ao = run(second, **kw)
        print(f"{name:<36} {wr_in:>11.1f}% {wr_out:>12.1f}% {ao:>+7.3f} (n={no})")
