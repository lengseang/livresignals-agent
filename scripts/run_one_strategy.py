#!/usr/bin/env python3
"""One frozen strategy through the walk-forward engine. Nine years, 13 symbols.

THE RULE IS FIXED HERE, BEFORE THE RUN. It is deliberately simple so the result
is about the engine and the data, not about parameter tuning. Nothing in this
file is adjusted after seeing results; if it were, it would be recorded as a
deviation with a reason.

  ENTRY (long only, spot):
      close > SMA(50)                      the slow trend is up
      and RSI(14) < 35                     short-term oversold within it
      and close > close 4 bars ago         turning, not still falling

  EXIT:
      stop   = -1.2% from fill             (the tight regime the live book uses)
      target = +3.0% from fill             (RR 2.5)
      time   = 48 bars (12h on 15m)        prevents zombie positions

  SIZING: fixed notional, one position per symbol, max 10 concurrent.

That is the whole strategy. No look-ahead: every indicator is computed from
`history()` at the cursor, and the engine fills at the next bar's open.

Usage:  python3 scripts/run_one_strategy.py [--tf 15m] [--start 2017-08-17]
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

SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "ADA/USDT", "XRP/USDT",
           "LINK/USDT", "DOGE/USDT", "SOL/USDT", "AVAX/USDT", "ARB/USDT"]

# --- FROZEN PARAMETERS -----------------------------------------------------
SMA_N = 50
RSI_N = 14
RSI_MAX = 35.0
MOM_LOOKBACK = 4
STOP_FRAC = 0.012
TARGET_FRAC = 0.030
HOLD_BARS = 48
NOTIONAL = 5.0
MAX_OPEN = 10
# ---------------------------------------------------------------------------


def sma(vals: list[float], n: int) -> float | None:
    return sum(vals[-n:]) / n if len(vals) >= n else None


def rsi(vals: list[float], n: int) -> float | None:
    """Wilder-style RSI using ONLY the values given (all <= cursor)."""
    if len(vals) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(len(vals) - n, len(vals)):
        d = vals[i] - vals[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100.0 - (100.0 / (1.0 + rs))


def make_decider():
    def decide(feeds, openpos):
        out = []
        for sym, feed in feeds.items():
            if sym in openpos:
                continue
            bars = feed.history(SMA_N + 2)
            if len(bars) < SMA_N + 2:
                continue
            closes = [b.close for b in bars]
            s = sma(closes, SMA_N)
            r = rsi(closes, RSI_N)
            if s is None or r is None:
                continue
            c = closes[-1]
            mom = closes[-1] > closes[-1 - MOM_LOOKBACK]
            if c > s and r < RSI_MAX and mom:
                out.append(Decision(sym, "enter_long", stop_frac=STOP_FRAC,
                                    target_frac=TARGET_FRAC, hold_bars=HOLD_BARS,
                                    tag="sma50-rsi35-mom4"))
        return out
    return decide


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--start", default="2017-08-17")
    ap.add_argument("--end", default="2026-10-01")
    args = ap.parse_args()

    def ms(d: str) -> int:
        return int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    import time
    t0 = time.time()
    eng = ReplayEngine(con, SYMBOLS, args.tf, ms(args.start), ms(args.end),
                       costs=Costs(), notional=NOTIONAL, max_open=MAX_OPEN)
    trades = eng.run(make_decider())
    dt_s = time.time() - t0

    print(f"=== frozen strategy over {args.tf}, {args.start} .. {args.end} ===")
    print(f"symbols {len(SYMBOLS)}   bars/symbol ~{eng.feeds[SYMBOLS[0]].series_len()}")
    print(f"elapsed {dt_s:.1f}s\n")

    if not trades:
        print("  no trades")
        return 0

    n = len(trades)
    net = sum(t.pnl for t in trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gw = sum(t.pnl for t in wins)
    gl = abs(sum(t.pnl for t in losses)) or 1e-9

    print(f"  n={n}  win={len(wins)/n*100:.1f}%  net={net:+.3f}  "
          f"avg={net/n:+.5f}  PF={gw/gl:.2f}")

    rc = defaultdict(lambda: [0, 0.0])
    for t in trades:
        rc[t.reason][0] += 1
        rc[t.reason][1] += t.pnl
    print("\n  by exit reason:")
    for k, (c, s) in sorted(rc.items(), key=lambda x: -x[1][0]):
        print(f"    {k:6} n={c:5} net={s:+9.3f} avg={s/c:+.5f}")

    print("\n  by symbol:")
    sc = defaultdict(lambda: [0, 0.0, 0])
    for t in trades:
        sc[t.symbol][0] += 1
        sc[t.symbol][1] += t.pnl
        if t.pnl > 0:
            sc[t.symbol][2] += 1
    for s, (c, net2, w) in sorted(sc.items(), key=lambda x: x[1][1]):
        print(f"    {s:10} n={c:5} win={w/c*100:5.1f}% net={net2:+9.3f} avg={net2/c:+.5f}")

    print("\n  by year:")
    yc = defaultdict(lambda: [0, 0.0, 0])
    for t in trades:
        y = dt.datetime.fromtimestamp(t.closed_ms / 1000, dt.timezone.utc).year
        yc[y][0] += 1
        yc[y][1] += t.pnl
        if t.pnl > 0:
            yc[y][2] += 1
    for y in sorted(yc):
        c, net2, w = yc[y]
        print(f"    {y}  n={c:5} win={w/c*100:5.1f}% net={net2:+9.3f} avg={net2/c:+.5f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
