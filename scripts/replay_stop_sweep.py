#!/usr/bin/env python3
"""Replay paper trades against real candles to find the best initial stop width.

Read-only. Does not touch the paper book, config, or any order path.

For each closed paper trade we take its RECORDED entry, tp, tf, symbol and open
time, then walk the real candle series from entry onward and simulate what would
have happened with a different INITIAL STOP distance:

    - if the bar low breaches entry*(1-w) before the target or the time stop,
      the trade stops out at that price
    - if the bar high reaches tp first, it takes profit
    - otherwise it exits at the RECORDED time stop (hold hours), at that bar's close

Options, slippage, funding and the lock-half ratchet are NOT modelled. This
isolates the initial stop width only.

Usage: python3 scripts/replay_stop_sweep.py
"""
from __future__ import annotations

import json
import sqlite3
import statistics as stats
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
DB = BASE / "data" / "trading.db"
PAPER = BASE / "data" / "unified-paper.json"
COST_ROUND_TRIP = 0.0028          # 0.28%, as the paper book assumes
WIDTHS = [0.008, 0.010, 0.012, 0.015, 0.020, 0.025, 0.030]
HOLD_HOURS = 6.0                  # paper book's time stop (from its own comments)

TF_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}


def load_candles(con, symbol, tf, t0_ms, t1_ms):
    rows = con.execute(
        "select open_time_utc, high, low, close from candles "
        "where symbol=? and timeframe=? and open_time_utc>=? and open_time_utc<=? "
        "order by open_time_utc",
        (symbol, tf, t0_ms, t1_ms),
    ).fetchall()
    return rows


def replay(entry, tp, lows, highs, closes, stop_w, hold_bars):
    """Return (exit_price, reason). Path-ordered; stop checked before target (pessimistic)."""
    sl = entry * (1.0 - stop_w)
    for i, (lo, hi) in enumerate(zip(lows, highs)):
        if lo is not None and lo <= sl:
            return sl, "SL"
        if hi is not None and hi >= tp:
            return tp, "TP"
        if i + 1 >= hold_bars:
            return closes[i], "TIME"
    if closes:
        return closes[-1], "TIME"
    return None, "NODATA"


def main() -> None:
    d = json.loads(PAPER.read_text())
    trades = [r for r in d["closed"] if "pnl" in r]
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    usable, skipped = [], 0
    for r in trades:
        tf = r.get("tf")
        if tf not in TF_MS:
            skipped += 1
            continue
        ts = r.get("ts_utc", "")
        if len(ts) < 19:
            skipped += 1
            continue
        import datetime as dt
        try:
            t0 = dt.datetime.fromisoformat(ts)
        except Exception:
            skipped += 1
            continue
        t0_ms = int(t0.timestamp() * 1000)
        step = TF_MS[tf]
        hold_bars = max(1, int(HOLD_HOURS * 3_600_000 / step))
        rows = load_candles(con, r["symbol"], tf, t0_ms, t0_ms + (hold_bars + 2) * step)
        if not rows:
            skipped += 1
            continue
        usable.append({"r": r, "rows": rows, "hold_bars": hold_bars,
                       "entry": r["entry"], "tp": r.get("tp"), "size": r.get("size", 5.0)})

    print(f"trades with pnl      : {len(trades)}")
    print(f"replayable           : {len(usable)}")
    print(f"skipped (no tf/candles): {skipped}")
    if not usable:
        print("nothing to replay")
        return

    print(f"\n{'width':>7} {'n':>5} {'win%':>6} {'net':>9} {'avg':>10} "
          f"{'SL%':>6} {'TP%':>6} {'TIME%':>6}")
    results = {}
    for w in WIDTHS:
        pnl = []
        reasons = {"SL": 0, "TP": 0, "TIME": 0, "NODATA": 0}
        for t in usable:
            lows = [x[1] for x in t["rows"]]
            highs = [x[2] for x in t["rows"]]
            closes = [x[3] for x in t["rows"]]
            if not t["tp"]:
                continue
            px, why = replay(t["entry"], t["tp"], lows, highs, closes, w, t["hold_bars"])
            reasons[why] = reasons.get(why, 0) + 1
            if px is None:
                continue
            gross = (px / t["entry"] - 1.0)
            net = gross - COST_ROUND_TRIP
            pnl.append(net * t["size"])
        if not pnl:
            continue
        n = len(pnl)
        net = sum(pnl)
        w_ = sum(1 for x in pnl if x > 0)
        results[w] = {"n": n, "net": net, "avg": net / n, "win": w_ / n * 100, "reasons": reasons}
        rc = reasons
        tot = sum(v for k, v in rc.items() if k != "NODATA") or 1
        print(f"{w*100:6.1f}% {n:5} {w_/n*100:5.1f}% {net:+9.3f} {net/n:+10.5f} "
              f"{rc['SL']/tot*100:5.1f}% {rc['TP']/tot*100:5.1f}% {rc['TIME']/tot*100:5.1f}%")

    if results:
        best = max(results.items(), key=lambda kv: kv[1]["avg"])
        print(f"\nbest average per trade: {best[0]*100:.1f}%  ({best[1]['avg']:+.5f})")
        base = results.get(0.012)
        if base:
            print(f"vs current 1.2%        : {base['avg']:+.5f}")
        base2 = results.get(0.020)
        if base2:
            print(f"vs the 2.0% fallback   : {base2['avg']:+.5f}")


if __name__ == "__main__":
    main()
