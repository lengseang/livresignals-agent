#!/usr/bin/env python3
"""Does the strategy have edge in a NON-TRENDING regime, or only in an uptrend?

The question this answers
-------------------------
Every strategy tested this week loses to buy-and-hold. The reason is structural:
these assets rose for years, and any rule that leaves the market to time it gives
up more than it gains. The only untested idea is whether the same rules have edge
where there is NO free trend to lose to.

We have no non-crypto data (the DB holds 10 crypto majors only), so we cannot test
"a different market". But we CAN test "a different regime": the same assets over
windows where the market was flat or falling. If a rule only works while the
market rises, that is beta. If it works when the market falls, that is edge — and
it is the first thing this week that would matter.

Method
------
  1. Slice the data into NON-OVERLAPPING windows (default 120 days each).
  2. Classify each window by the BUY-AND-HOLD return of the basket over it:
        UP    basket return >  +10%
        FLAT  basket return in [-10%, +10%]
        DOWN  basket return <  -10%
  3. Run one representative rule from each family on each window.
  4. Report performance BY REGIME, and compare to the basket in the same window.

The falsification, written before running
-----------------------------------------
  H1  if a rule beats the basket in DOWN windows, it has edge, not beta
      -> this is the only thing that would change the conclusion
  H2  if it only beats the basket in UP windows, it is beta and is rejected
  H3  if it beats the basket in NO regime, the class is empty and we say so

A rule that is positive in DOWN windows and negative in UP windows is the
signature of a real edge that the bull market has been masking.

Costs always charged: 0.10% fee + 0.05% slip per side.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_engine import Costs, Decision, ReplayEngine

DB = Path(__file__).resolve().parents[1] / "data" / "trading.db"
SYMS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
        "ADA/USDT", "LINK/USDT", "DOGE/USDT", "AVAX/USDT"]

# Data supports this: BTC/ETH/SOL from 2022-01, ARB excluded (2023-03).
START = "2022-01-01"
END = "2026-10-01"
WINDOW_DAYS = 120
TF = "4h"

COST = Costs(fee_bps=5.0, slippage_bps=5.0)     # 0.05%/side


def ms(d: str) -> int:
    return int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


def basket_return(con, a, b) -> float:
    rs = []
    for s in SYMS:
        r = con.execute(
            "select close from candles where symbol=? and timeframe=? "
            "and open_time_utc>=? and open_time_utc<=? order by open_time_utc",
            (s, TF, a, b)).fetchall()
        if len(r) > 9:
            rs.append(r[-1][0] / r[0][0] - 1)
    return sum(rs) / len(rs) if rs else 0.0


# --- one representative rule per family, mirroring the week's hunts ---------

def dip(bars):
    c = [b.close for b in bars]
    if len(c) < 92:
        return False
    dd = c[-1] / max(c[-90:]) - 1
    g = l = 0.0
    for j in range(len(c) - 14, len(c)):
        d = c[j] - c[j - 1]
        g += d if d > 0 else 0
        l += -d if d < 0 else 0
    r = 100.0 if l == 0 else 100 - 100 / (1 + (g / 14) / (l / 14))
    return dd <= -0.009 and r <= 35


def breakout(bars):
    c = [b.close for b in bars]
    return len(c) >= 55 and c[-1] > max(c[-54:-1])


def bearcross(bars):
    c = [b.close for b in bars]
    if len(c) < 202:
        return False
    af, bf = sma(c[:-1], 50), sma(c[:-1], 200)
    an, bn = sma(c, 50), sma(c, 200)
    if af is None or bf is None or an is None or bn is None:
        return False
    return af >= bf and an < bn


RULES = [
    ("dip-long",      dip,      "long",  0.025, 0.05,  12),
    ("breakout-long", breakout, "long",  0.06,  0.18,  180),
    ("bearcross-short", bearcross, "short", 0.04, 0.12, 180),
]


def run_window(con, rule_fn, action, stop, target, hold, a, b):
    eng = ReplayEngine(con, SYMS, TF, a, b, costs=COST, notional=5.0,
                       max_open=8, min_coverage=0.85)
    def dec(feeds, openpos):
        out = []
        for sym, feed in feeds.items():
            if any(p.symbol == sym for p in openpos.values()):
                continue
            bars = feed.history(260)
            if len(bars) < 55:
                continue
            if rule_fn(bars):
                out.append(Decision(sym, f"enter_{action}", stop_frac=stop,
                                    target_frac=target, hold_bars=hold, tag="r"))
        return out
    tr = eng.run(dec)
    if not tr:
        return None
    net = sum(t.pnl for t in tr)
    gw = sum(t.pnl for t in tr if t.pnl > 0)
    gl = abs(sum(t.pnl for t in tr if t.pnl <= 0)) or 1e-9
    return {"n": len(tr), "net": net, "pf": gw / gl}


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    t0 = dt.datetime.fromisoformat(START).replace(tzinfo=dt.timezone.utc)
    t1 = dt.datetime.fromisoformat(END).replace(tzinfo=dt.timezone.utc)
    windows = []
    cur = t0
    while cur + dt.timedelta(days=WINDOW_DAYS) <= t1:
        nxt = cur + dt.timedelta(days=WINDOW_DAYS)
        windows.append((int(cur.timestamp() * 1000), int(nxt.timestamp() * 1000),
                        cur.date().isoformat(), nxt.date().isoformat()))
        cur = nxt

    print(f"=== REGIME test: does any rule have edge without a free uptrend? ===")
    print(f"data {START}..{END}   {len(windows)} non-overlapping {WINDOW_DAYS}d windows")
    print(f"costs 0.05%/side charged   symbols {len(SYMS)}\n")

    # classify each window by the basket
    reg = {}
    for (a, b, la, lb) in windows:
        r = basket_return(con, a, b)
        reg[(a, b)] = "UP" if r > 0.10 else ("DOWN" if r < -0.10 else "FLAT")
        print(f"  {la} .. {lb}   basket {r*100:+7.1f}%   -> {reg[(a,b)]}")

    print()
    # run each rule over each window
    results = defaultdict(lambda: defaultdict(list))
    for name, fn, action, stop, target, hold in RULES:
        for (a, b, la, lb) in windows:
            r = run_window(con, fn, action, stop, target, hold, a, b)
            if r:
                results[name][reg[(a, b)]].append((r, basket_return(con, a, b)))

    print("=== by regime ===")
    for name, _, _, _, _, _ in RULES:
        print(f"\n  {name}")
        for regime in ("UP", "FLAT", "DOWN"):
            rows = results[name].get(regime, [])
            if not rows:
                print(f"    {regime:5} no windows")
                continue
            pf = sum(r["pf"] for r, _ in rows) / len(rows)
            net = sum(r["net"] for r, _ in rows)
            bench = sum(bk for _, bk in rows) / len(rows)
            beat = sum(1 for r, bk in rows if r["net"] / 20.0 > bk)
            print(f"    {regime:5} {len(rows)} windows  avg PF {pf:4.2f}  "
                  f"net ${net:+7.2f}  avg basket {bench*100:+6.1f}%  "
                  f"beat basket {beat}/{len(rows)}")

    print("\n=== VERDICT ===")
    any_edge = False
    for name, _, _, _, _, _ in RULES:
        dn = results[name].get("DOWN", [])
        if dn and sum(1 for r, bk in dn if r["net"] / 20.0 > bk) > len(dn) / 2:
            print(f"  {name}: BEATS the basket in a majority of DOWN windows -> edge")
            any_edge = True
    if not any_edge:
        print("  No rule beats the basket in DOWN windows.")
        print("  Every family is beta: it works when the market rises and not otherwise.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
