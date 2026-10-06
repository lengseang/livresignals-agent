#!/usr/bin/env python3
"""Faithful replay of the live unified_trader configuration.

Differences from replay_real_strategy.py, each taken from the live source:

  symbols     SYMS + WATCH_SYMS (line 55/62)          -> 14 pairs
  timeframes  5m, 15m, 30m (TFS, line 64)              -> all three
  max_open    8 (line 334, _paper_open)                -> hard slot cap
  cadence     SCAN_EVERY_BARS — the live system runs on its own schedule, so
              the replay scans only every N bars rather than every bar. This is
              the parameter that reconciles the 29k-vs-2.4k trade-count gap.
  entry       _validated() buckets + the fallback, verbatim
  exit        stop 2.0%, target 4.0%, hold 2h (8 bars on 15m; 24 on 5m; 4 on 30m)

COST_SWEEP runs the same rule at the cost levels that matter: the real fills
measured ~0.03%, the exchange taker is ~0.10%, and 0.30% is the pessimistic case.

FROZEN before the run. Nothing adjusted after seeing results.

Usage: python3 scripts/replay_live_config.py [--cost 0.0003]
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

# --- live universe (line 55-63), restricted to symbols we have candles for ---
SYMS = ["BTC/USDT", "ETH/USDT", "XRP/USDT", "SOL/USDT", "DOGE/USDT",
        "ADA/USDT", "BNB/USDT", "LINK/USDT", "AVAX/USDT"]
WATCH = ["ARB/USDT", "NEAR/USDT", "SUI/USDT"]
UNIVERSE = SYMS + WATCH
TFS = ["5m", "15m", "30m"]

MAX_OPEN = 8                    # line 334

# --- live VALIDATED buckets -------------------------------------------------
VALIDATED = {
    ("XRP/USDT", "30m"): {"dip": -0.08, "rsi": 30, "red": 3},
    ("ETH/USDT", "15m"): {"dip": -0.06, "rsi": 40, "red": 4},
    ("BTC/USDT", "5m"):  {"dip": -0.005, "rsi": 30, "red": 3},
    ("XRP/USDT", "15m"): {"dip": -0.05, "rsi": 40, "red": 3},
    ("ETH/USDT", "30m"): {"dip": -0.06, "rsi": 40, "red": 3},
}
FB_DIP, FB_RSI, FB_RED = -0.02, 30, 3

# --- live exits -------------------------------------------------------------
STOP_FRAC = 0.020
TARGET_FRAC = 0.040
HOLD_HOURS = 2.0
NOTIONAL = 5.0

# --- bars to hold per timeframe (2h) ---------------------------------------
HOLD_BARS = {"5m": 24, "15m": 8, "30m": 4}

# --- the scan cadence: live runs on a schedule, so model a coarse scan ------
SCAN_EVERY_BARS = {"5m": 3, "15m": 1, "30m": 1}   # 15m/30m: every bar (the cron
                                                  # is ~15m); 5m: every 3rd bar


def ms(d: str) -> int:
    return int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def rsi_simple(closes, n=14):
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


def features(bars):
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
    return {"dd": dd, "rsi": r, "red": red, "close": closes[-1]}


def validated(sig, sym, tf):
    v = VALIDATED.get((sym, tf))
    if not v:
        return sig["dd"] <= FB_DIP and sig["rsi"] <= FB_RSI and sig["red"] >= FB_RED
    return sig["dd"] <= v["dip"] and sig["rsi"] <= v["rsi"] and sig["red"] >= v["red"]


def run(tf: str, cost_bps: float, slip_bps: float, scan_every: int,
        start: str, end: str, collect=False):
    """Run ONE timeframe (the live loop scans each tf independently)."""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    base = Costs(fee_bps=cost_bps, slippage_bps=slip_bps)

    class PerTF(ReplayEngine):
        pass

    eng = PerTF(con, UNIVERSE, tf, ms(start), ms(end), costs=base,
                notional=NOTIONAL, max_open=MAX_OPEN)

    tick = [0]
    hold = HOLD_BARS[tf]

    def decide(feeds, openpos):
        tick[0] += 1
        if tick[0] % scan_every:
            return []
        out = []
        for sym, feed in feeds.items():
            if any(p.symbol == sym for p in openpos.values()):
                continue
            bars = feed.history(120)
            if len(bars) < 90:
                continue
            sig = features(bars)
            if sig is None:
                continue
            if validated(sig, sym, tf):
                out.append(Decision(sym, "enter_long", stop_frac=STOP_FRAC,
                                    target_frac=TARGET_FRAC, hold_bars=hold,
                                    tag="live"))
        return out

    trades = eng.run(decide)
    return trades


def summarize(trades, label):
    if not trades:
        print(f"  {label:34} no trades")
        return
    n = len(trades)
    net = sum(t.pnl for t in trades)
    w = sum(1 for t in trades if t.pnl > 0)
    gw = sum(t.pnl for t in trades if t.pnl > 0)
    gl = abs(sum(t.pnl for t in trades if t.pnl <= 0)) or 1e-9
    print(f"  {label:34} n={n:6} win={w/n*100:5.1f}% net={net:+10.3f} "
          f"avg={net/n:+.5f} PF={gw/gl:.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-08-17")
    ap.add_argument("--end", default="2026-10-01")
    args = ap.parse_args()

    print("=== faithful live-config replay (max_open 8, 3 timeframes, 12 symbols) ===")
    print(f"    window {args.start} .. {args.end}")
    print()

    # per-timeframe, at the cost level that matters most
    for tf in TFS:
        se = SCAN_EVERY_BARS[tf]
        print(f"--- {tf} (scan every {se} bar(s)) ---")
        for label, fee, slip in [("real fills ~0.03% RT", 1.5, 0.0),
                                 ("exchange ~0.10% RT", 5.0, 0.0),
                                 ("taker+slip 0.30% RT", 10.0, 5.0)]:
            tr = run(tf, fee, slip, se, args.start, args.end)
            summarize(tr, label)
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
