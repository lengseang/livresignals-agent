"""Comprehensive lever sweep — find every win-rate improvement, then combine.

Tests the remaining levers on 1h BTC data (13,510 candles):
  - volume confirmation (high volume dip = stronger signal?)
  - time-of-day (dips at certain hours revert better?)
  - RSI (oversold = better reversion?)
  - consecutive red bars (more red = stronger mean reversion?)
  - Bollinger width (squeeze vs expansion)

Each lever is tested ALONE (with the shallow-dip + high-vol base) to isolate
its marginal contribution. Then the best interactions are validated OOS.
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
    if i < lookback:
        return 0.0
    rets = [rows[j]['close']/rows[j-1]['close']-1 for j in range(i-lookback+1, i)]
    return statistics.pstdev(rets) if len(rets) > 1 else 0.0


def rsi_at(rows, i, lookback=14):
    if i < lookback:
        return 50.0
    gains = 0.0; losses = 0.0
    for j in range(i-lookback+1, i+1):
        d = rows[j]['close'] - rows[j-1]['close']
        if d >= 0: gains += d
        else: losses -= d
    if losses == 0: return 100.0
    rs = (gains/lookback) / (losses/lookback)
    return 100.0 - 100.0/(1.0+rs)


def bb_width(rows, i, lookback=20):
    if i < lookback:
        return 0.0
    seg = [rows[j]['close'] for j in range(i-lookback, i)]
    mean = statistics.mean(seg)
    sd = statistics.pstdev(seg)
    return sd/mean if mean else 0.0


def vol_ratio(rows, i):
    """Current volume vs 30-bar median."""
    if i < 30:
        return 1.0
    vols = [rows[j]['volume'] for j in range(i-30, i)]
    med = statistics.median(vols)
    return rows[i]['volume']/med if med else 1.0


def run(rows, hold=48, dip_thresh=-0.05, vol_min=0.010,
        rsi_max=None, vol_ratio_min=None, hour_filter=None, red_bars_min=None,
        bb_mode=None, bb_thresh=None):
    wins=0; total=0; rets=[]
    for i in range(90, len(rows)-hold):
        hi = max(r['close'] for r in rows[i-90:i])
        dd = rows[i]['close']/hi - 1
        if dd > dip_thresh:
            continue
        v = vol_at(rows, i)
        if v < vol_min:
            continue
        if rsi_max is not None and rsi_at(rows, i) > rsi_max:
            continue
        if vol_ratio_min is not None and vol_ratio(rows, i) < vol_ratio_min:
            continue
        if hour_filter is not None:
            h = datetime.fromtimestamp(rows[i]['open_time_utc']/1000, tz=timezone.utc).hour
            if h not in hour_filter:
                continue
        if red_bars_min is not None:
            reds = sum(1 for j in range(max(1,i-red_bars_min), i+1)
                       if rows[j]['close'] < rows[j-1]['close'])
            if reds < red_bars_min:
                continue
        if bb_mode is not None:
            bw = bb_width(rows, i)
            if bb_mode == "squeeze" and bw > bb_thresh:
                continue
            if bb_mode == "expansion" and bw < bb_thresh:
                continue
        total += 1
        fwd = rows[i+hold]['close']/rows[i]['close'] - 1
        rets.append(fwd)
        if fwd > 0: wins += 1
    wr = wins/total*100 if total else 0
    avg = sum(rets)/len(rets)*100 if rets else 0
    return wr, total, avg


if __name__ == "__main__":
    rows = load_rows()
    print(f"1h data: {len(rows)} candles")
    print()
    base = run(rows)  # baseline: shallow dip -5% + high vol
    print(f"=== BASELINE (dip -5% + high vol, hold 48h): WR {base[0]:.1f}% n={base[1]} avg {base[2]:+.3f}% ===")
    print()
    print("=== RSI FILTER ===")
    for rmax in [30, 40, 50]:
        wr,n,a = run(rows, rsi_max=rmax)
        print(f"  RSI < {rmax}: WR {wr:.1f}% n={n} avg {a:+.3f}%")
    print("=== VOLUME FILTER ===")
    for vr in [1.5, 2.0, 3.0]:
        wr,n,a = run(rows, vol_ratio_min=vr)
        print(f"  vol ratio > {vr}: WR {wr:.1f}% n={n} avg {a:+.3f}%")
    print("=== TIME OF DAY ===")
    for hf, name in [({0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23}, "all hours")]:
        pass
    # UTC hours grouped
    for hf, name in [({0,1,2,3,4,5}, "0-6 UTC"), ({6,7,8,9,10,11}, "6-12 UTC"),
                     ({12,13,14,15,16,17}, "12-18 UTC"), ({18,19,20,21,22,23}, "18-24 UTC")]:
        wr,n,a = run(rows, hour_filter=hf)
        print(f"  {name}: WR {wr:.1f}% n={n} avg {a:+.3f}%")
    print("=== CONSECUTIVE RED BARS ===")
    for rb in [2, 3]:
        wr,n,a = run(rows, red_bars_min=rb)
        print(f"  >= {rb} red bars: WR {wr:.1f}% n={n} avg {a:+.3f}%")
    print("=== BOLLINGER WIDTH ===")
    for mode, th, name in [("squeeze", 0.01, "squeeze <1%"), ("expansion", 0.02, "expansion >2%")]:
        wr,n,a = run(rows, bb_mode=mode, bb_thresh=th)
        print(f"  {name}: WR {wr:.1f}% n={n} avg {a:+.3f}%")
