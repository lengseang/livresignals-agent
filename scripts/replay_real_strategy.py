#!/usr/bin/env python3
"""Replay the REAL unified_trader entry rule through the walk-forward engine.

This is not an invented strategy. It reproduces the logic that actually runs on
this box, read from scripts/unified_trader.py:

  _features():  dd   = close/max(close[-90:]) - 1        (drawdown from 90-bar high)
                rsi  = 14-bar RSI (simple, matching the live code)
                red  = consecutive red closes
                ema  = EMA over the window, slope over last 4 bars

  _validated(): per (symbol, tf) bucket: dd <= dip AND rsi <= rsi AND red >= red
                else the general fallback: dd <= -0.02 AND rsi <= 30 AND red >= 3

  _paper_open(): stop from data/exit-params.json (2.0%), target = stop * 2 => RR 2.0

The rule is FROZEN before the run. The thresholds are the live ones; the fallback
is the live fallback. Two things are necessarily approximations and are stated:

  1. The live code fetches limit=120 bars and computes over a rolling window. Here
     the same window sizes are used (90, 14, EMA over all provided bars). Minor
     differences in seed length are possible, noted but not chased.
  2. VALIDATED buckets only exist for 5 (symbol, tf) pairs. For every other pair
     the live system uses the FALLBACK rule. Both are modelled.

Usage: python3 scripts/replay_real_strategy.py [--tf 15m] [--costs 10,5]
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
from collections import defaultdict
from pathlib import Path

from locked_feed import LockedFeed
from replay_engine import Costs, Decision, ReplayEngine

DB = Path(__file__).resolve().parents[1] / "data" / "trading.db"

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
           "ADA/USDT", "LINK/USDT", "DOGE/USDT", "AVAX/USDT", "NEAR/USDT",
           "SUI/USDT", "ARB/USDT", "DOT/USDT", "LTC/USDT", "ATOM/USDT"]

# --- the live VALIDATED buckets, copied verbatim ---------------------------
VALIDATED = {
    ("XRP/USDT", "30m"): {"dip": -0.08, "rsi": 30, "red": 3},
    ("ETH/USDT", "15m"): {"dip": -0.06, "rsi": 40, "red": 4},
    ("BTC/USDT", "5m"):  {"dip": -0.005, "rsi": 30, "red": 3},
    ("XRP/USDT", "15m"): {"dip": -0.05, "rsi": 40, "red": 3},
    ("ETH/USDT", "30m"): {"dip": -0.06, "rsi": 40, "red": 3},
}
# --- the live fallback ------------------------------------------------------
FB_DIP, FB_RSI, FB_RED = -0.02, 30, 3

# --- live exit params: data/exit-params.json says stop 2.0%, tp = stop*2 -----
STOP_FRAC = 0.020
TARGET_FRAC = 0.040
HOLD_BARS_DEFAULT = 48          # 12h on 15m; the live hold is 2h per exit-params,
                                # so also run with HOLD_BARS_FROM_PARAMS below
HOLD_FROM_PARAMS_H = 2.0

NOTIONAL = 5.0
MAX_OPEN = 10


def ms(d: str) -> int:
    return int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def rsi_simple(closes: list[float], n: int = 14) -> float:
    """Matches the live code: average gain/loss over the last n deltas."""
    if len(closes) < n + 1:
        return 50.0
    g = l = 0.0
    for j in range(len(closes) - n, len(closes)):
        dx = closes[j] - closes[j - 1]
        if dx > 0:
            g += dx
        else:
            l -= dx
    return 100.0 if l == 0 else 100 - 100 / (1 + (g / n) / (l / n))


def features(bars) -> dict | None:
    closes = [b.close for b in bars]
    if len(closes) < 90:
        return None
    hi = max(closes[-90:])
    dd = closes[-1] / hi - 1
    r = rsi_simple(closes, 14)
    red = 0
    k = len(closes) - 1
    while k > 0 and closes[k] < closes[k - 1]:
        red += 1
        k -= 1
    mult = 2 / (min(40, len(closes)) + 1)
    ema = 0.0
    for c in closes:
        ema = c * mult + ema * (1 - mult)
    ema_prev = 0.0
    for c in closes[:-4]:
        ema_prev = c * mult + ema_prev * (1 - mult)
    slope = (ema - ema_prev) / (ema_prev or 1)
    return {"dd": dd, "rsi": r, "red": red, "close": closes[-1], "trend": slope}


def validated(sig: dict, sym: str, tf: str) -> bool:
    v = VALIDATED.get((sym, tf))
    if not v:
        return sig["dd"] <= FB_DIP and sig["rsi"] <= FB_RSI and sig["red"] >= FB_RED
    return sig["dd"] <= v["dip"] and sig["rsi"] <= v["rsi"] and sig["red"] >= v["red"]


def make_decider(tf: str, hold_bars: int):
    def decide(feeds, openpos):
        out = []
        for sym, feed in feeds.items():
            if sym in openpos:
                continue
            bars = feed.history(120)
            if len(bars) < 90:
                continue
            sig = features(bars)
            if sig is None:
                continue
            if validated(sig, sym, tf):
                out.append(Decision(sym, "enter_long", stop_frac=STOP_FRAC,
                                    target_frac=TARGET_FRAC, hold_bars=hold_bars,
                                    tag="unified-native"))
        return out
    return decide


def report(trades, label):
    if not trades:
        print(f"  {label}: no trades")
        return
    n = len(trades)
    net = sum(t.pnl for t in trades)
    w = sum(1 for t in trades if t.pnl > 0)
    gw = sum(t.pnl for t in trades if t.pnl > 0)
    gl = abs(sum(t.pnl for t in trades if t.pnl <= 0)) or 1e-9
    print(f"  {label:38} n={n:5} win={w/n*100:5.1f}% net={net:+9.3f} "
          f"avg={net/n:+.5f} PF={gw/gl:.2f}")
    return net


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--start", default="2017-08-17")
    ap.add_argument("--end", default="2026-10-01")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    print(f"=== REAL unified_trader rule, {args.tf} ===")
    print(f"    entry: dd<=-0.02 & rsi<=30 & red>=3 (fallback) or the per-symbol buckets")
    print(f"    exit : stop {STOP_FRAC:.1%}  target {TARGET_FRAC:.1%}  (live exit-params)")
    print()

    # the live hold on 15m: exit-params says 2.0h => 8 bars
    hold_2h = max(1, int(HOLD_FROM_PARAMS_H * 3_600_000 / (15 * 60_000)))  # 8 bars

    for label, hold, costs in [
        ("hold 12h (48 bars), cost .30% RT", 48, Costs(10, 5)),
        ("hold 2h (8 bars),   cost .30% RT", hold_2h, Costs(10, 5)),
        ("hold 2h (8 bars),   cost .10% RT", hold_2h, Costs(5, 0)),
        ("hold 2h (8 bars),   NO cost", hold_2h, Costs(0, 0)),
    ]:
        eng = ReplayEngine(con, SYMBOLS, args.tf, ms(args.start), ms(args.end),
                           costs=costs, notional=NOTIONAL, max_open=MAX_OPEN)
        trades = eng.run(make_decider(args.tf, hold))
        report(trades, label)

    # detail for the most realistic config
    print()
    eng = ReplayEngine(con, SYMBOLS, args.tf, ms(args.start), ms(args.end),
                       costs=Costs(5, 0), notional=NOTIONAL, max_open=MAX_OPEN)
    trades = eng.run(make_decider(args.tf, hold_2h))
    if trades:
        rc = defaultdict(lambda: [0, 0.0])
        for t in trades:
            rc[t.reason][0] += 1
            rc[t.reason][1] += t.pnl
        print("  detail (hold 2h, cost .10% RT) by exit:")
        for k, (c, s) in sorted(rc.items(), key=lambda x: -x[1][0]):
            print(f"    {k:6} n={c:5} net={s:+9.3f} avg={s/c:+.5f}")
        yc = defaultdict(lambda: [0, 0.0])
        for t in trades:
            y = dt.datetime.fromtimestamp(t.closed_ms / 1000, dt.timezone.utc).year
            yc[y][0] += 1
            yc[y][1] += t.pnl
        print("  by year:")
        for y in sorted(yc):
            c, s = yc[y]
            print(f"    {y}  n={c:5} net={s:+9.3f} avg={s/c:+.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
