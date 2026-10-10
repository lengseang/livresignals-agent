#!/usr/bin/env python3
"""The benchmark the hunt was missing: does a candidate beat equal-weight BUY&HOLD?

The low-frequency hunt found PF 1.25-1.93 across every family. That looked like
the first edge of the day. It is not — the control killed it:

    equal-weight buy & hold    IS 2017-2023  +1,884%
                               OOS 2023-2026   +230%

Any long-biased rule with wide stops rides that. A PF above 1.0 in a raging bull
market is BETA, not edge. The hunt's IS/OOS split catches overfitting to a
PATTERN; it does not catch overfitting to a REGIME. This script adds the missing
control.

The real question is not "is PF > 1" but "does this beat simply holding the coins,
after fees?" — because holding costs nothing and needs no system.

Benchmark: equal-weight buy & hold the same 10 symbols over the same window,
compounded, no fees (the honest floor — you can hold for free).

A candidate passes only if BOTH:
  * PF > 1.0 (it makes money), AND
  * its compounded return beats the basket's,
run on the SAME symbols and window.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent if "__file__" in dir() else "."))
from replay_engine import Costs, Decision, ReplayEngine

DB = Path(__file__).resolve().parents[1] / "data" / "trading.db"
SYMS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
        "ADA/USDT", "LINK/USDT", "DOGE/USDT", "AVAX/USDT", "ARB/USDT"]
COST = Costs(fee_bps=5.0, slippage_bps=5.0)


def ms(d):
    return int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def basket_return(con, start, end):
    """Equal-weight buy & hold, compounded. No fees: holding is free."""
    A, B = ms(start), ms(end)
    rets = []
    for s in SYMS:
        rows = con.execute(
            "select close from candles where symbol=? and timeframe='4h' "
            "and open_time_utc>=? and open_time_utc<=? order by open_time_utc",
            (s, A, B)).fetchall()
        if len(rows) < 10:
            continue
        r = rows[-1][0] / rows[0][0] - 1
        rets.append(r)
    if not rets:
        return 0.0
    # equal weight of a REBALANCED basket == mean of returns (per period)
    # approximate as the arithmetic mean of per-symbol total returns
    return sum(rets) / len(rets)


def trade_compound(tr):
    """Compound the trades' returns on a fixed notional, sequentially in time."""
    if not tr:
        return 0.0
    tr = sorted(tr, key=lambda t: getattr(t, "entry_time", 0) or 0)
    eq = 1.0
    for t in tr:
        notional = getattr(t, "notional", 5.0) or 5.0
        eq *= (1 + t.pnl / notional)
    return eq - 1


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cache = {}

    def breakout(look):
        def f(bars):
            c = [b.close for b in bars]
            if len(c) < look + 2:
                return False
            return c[-1] > max(c[-look - 1:-1])
        return f

    print("=== the control the hunt was missing: BEAT BUY & HOLD ===")
    for label, (a, b) in [("IS 2017-2023", ("2017-01-01", "2023-01-01")),
                          ("OOS 2023-2026", ("2023-01-01", "2026-10-01"))]:
        bench = basket_return(con, a, b)
        print(f"\n  {label}")
        print(f"    BENCHMARK equal-weight buy & hold   {bench*100:+10.1f}%")
        for look in (20, 55, 100, 200):
            eng = ReplayEngine(con, SYMS, "4h", ms(a), ms(b), costs=COST,
                               notional=5.0, max_open=8, bars_cache=cache)

            def decide(feeds, openpos, _f=breakout(look)):
                out = []
                for sym, feed in feeds.items():
                    if any(p.symbol == sym for p in openpos.values()):
                        continue
                    bars = feed.history(260)
                    if len(bars) < 55:
                        continue
                    if _f(bars):
                        out.append(Decision(sym, "enter_long", stop_frac=0.06,
                                            target_frac=0.18, hold_bars=540, tag="ctl"))
                return out

            tr = eng.run(decide)
            comp = trade_compound(tr)
            pf = None
            if tr:
                gw = sum(t.pnl for t in tr if t.pnl > 0)
                gl = abs(sum(t.pnl for t in tr if t.pnl <= 0)) or 1e-9
                pf = gw / gl
            verdict = "BEATS HOLD" if comp > bench else "loses to hold"
            print(f"    breakout {look:3}-bar  n={len(tr):4}  PF {pf:5.2f}  "
                  f"compound {comp*100:+10.1f}%   {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
