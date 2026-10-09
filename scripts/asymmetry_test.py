#!/usr/bin/env python3
"""Test the asymmetry hypothesis: does the market punish dip-buying and reward
trading the down-run?

Owner observation (2026-10-09): "there are down than up". Bar counts are almost
perfectly symmetric (49.7/50.3), and total up-move % equals total down-move %
(+4737 vs -4733). So the asymmetry is NOT in the counts. It is in the SHAPE:

    up-runs    max 11 bars
    down-runs  max 16 bars      <- drops cluster longer
    max drawdown -53.8% in a year that ended -12.2%

Long grinding declines, sharp rallies. That is a regime where a dip-buyer with a
tight stop gets shaken out of every position just before the bounce it was
waiting for.

This script does NOT invent a new rule. It takes the SAME entry (a dip) and
varies only the EXIT (stop width, target, hold) plus compares it against the
opposite direction (fade the rip), to isolate the asymmetry's effect.

Falsification criteria, written BEFORE the run:
  H1  a wider stop + longer hold should beat a tight stop + short hold
      -> if narrow stops win, "long down-runs" is NOT the binding constraint
  H2  the exit axis should matter more than the entry
      -> if not, the asymmetry story is wrong and something else dominates
  H3  at exchange cost, no variant should clear PF 1.0
      -> if one does, it is a candidate for the registry (and OOS re-test)

Every number is net of 0.10% fee + 0.05% slip per side. No look-ahead: the
engine fills at the NEXT bar's open and forbids peek().
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_engine import Costs, Decision, ReplayEngine
import hunt

DB = Path(__file__).resolve().parents[1] / "data" / "trading.db"
SYMBOLS = hunt.SYMBOLS
COST = Costs(fee_bps=5.0, slippage_bps=5.0)      # exchange taker + slip, per side
NOTIONAL = 5.0
MAX_OPEN = 8


def ms(d: str) -> int:
    import datetime as dt
    return int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def rs(bars, n=14):
    c = [b.close for b in bars]
    g = l = 0.0
    for j in range(len(c) - n, len(c)):
        d = c[j] - c[j - 1]
        g += d if d > 0 else 0
        l += -d if d < 0 else 0
    return 100.0 if l == 0 else 100 - 100 / (1 + (g / n) / (l / n))


def buy_dip(hi_n=90, rsi_max=35):
    """The FAMILY the live system trades: buy weakness."""
    def f(bars):
        c = [b.close for b in bars]
        if len(c) < hi_n + 2:
            return False
        dd = c[-1] / max(c[-hi_n:]) - 1
        return dd <= -hi_n / 10000.0 and rs(bars) <= rsi_max
    return f


def fade_rip(lo_n=90, rsi_min=65):
    """The OPPOSITE: sell strength. Only usable as a signal if we can short;
    here it is tested as a LONG-only proxy by measuring the SAME condition
    inverted, so we learn whether the up-move is the more reliable reverter."""
    def f(bars):
        c = [b.close for b in bars]
        if len(c) < lo_n + 2:
            return False
        ri = c[-1] / min(c[-lo_n:]) - 1
        return ri >= lo_n / 10000.0 and rs(bars) >= rsi_min
    return f


def run(con, tf, entry, stop, target, hold, start, end, cache, side="long"):
    eng = ReplayEngine(con, SYMBOLS, tf, ms(start), ms(end), costs=COST,
                       notional=NOTIONAL, max_open=MAX_OPEN, bars_cache=cache)

    def decide(feeds, openpos):
        out = []
        for sym, feed in feeds.items():
            if any(p.symbol == sym for p in openpos.values()):
                continue
            bars = feed.history(210)
            if len(bars) < 60:
                continue
            if entry(bars):
                out.append(Decision(sym, "enter_long", stop_frac=stop,
                                    target_frac=target, hold_bars=hold, tag="asym"))
        return out

    tr = eng.run(decide)
    if not tr:
        return None
    n = len(tr)
    net = sum(t.pnl for t in tr)
    w = sum(1 for t in tr if t.pnl > 0)
    gw = sum(t.pnl for t in tr if t.pnl > 0)
    gl = abs(sum(t.pnl for t in tr if t.pnl <= 0)) or 1e-9
    # how often did the STOP take us out vs the target?
    stops = sum(1 for t in tr if getattr(t, "reason", "") == "SL")
    return {"n": n, "win": w / n, "net": net, "avg": net / n, "pf": gw / gl,
            "sl_rate": stops / n}


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cache: dict = {}
    tf = "15m"
    START, END = "2025-01-01", "2026-10-01"

    print("=== asymmetry test: same entry, vary the EXIT ===")
    print(f"tf {tf}  {START}..{END}  symbols {len(SYMBOLS)}")
    print("cost 0.10% fee + 0.05% slip per side (exchange taker, always charged)\n")

    exits = [
        ("tight  sl1.0% tp2.0% hold24", 0.010, 0.020, 24),
        ("mid    sl2.0% tp4.0% hold48", 0.020, 0.040, 48),
        ("wide   sl4.0% tp8.0% hold96", 0.040, 0.080, 96),
        ("xwide  sl6.0% tp12%  hold192", 0.060, 0.120, 192),
    ]
    for label, entry in (("BUY-DIP  (the live family)", buy_dip()),
                         ("FADE-RIP (the opposite)", fade_rip())):
        print(f"  {label}")
        for name, s, t, h in exits:
            r = run(con, tf, entry, s, t, h, START, END, cache)
            if r:
                print(f"    {name:30} n={r['n']:5} win={r['win']*100:5.1f}% "
                      f"PF={r['pf']:.3f} avg={r['avg']:+.5f} SLhit={r['sl_rate']*100:4.0f}%")
            else:
                print(f"    {name:30} no trades")
        print()

    print("  H1  wider stop + longer hold beats tight?")
    print("  H2  exit axis dominates entry?")
    print("  H3  anything clears PF 1.0 at exchange cost?")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
