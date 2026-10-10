#!/usr/bin/env python3
"""The honest re-test of the bear-cross short, after finding the IS window was fiction.

WHAT WAS WRONG
Every result quoted on 2026-10-10 used "IS 2017-01-01 .. 2023-01-01". But the DB
does not hold that history for most symbols:

    BTC/USDT  starts 2022-01     ETH/USDT  starts 2022-01
    SOL/USDT  starts 2022-01     ARB/USDT  starts 2023-03
    XRP 2018-05  BNB 2017-11  ADA 2018-04  LINK 2019-01
    DOGE 2019-07  AVAX 2020-09

So the "in-sample" period was really 2022-2023 for BTC/ETH/SOL and 2023-only for
ARB. A rule that "worked 2017-2023" was tested on a window that, for a third of
the universe, barely existed. This script redoes it with a window the data
actually supports.

WHAT THIS DOES DIFFERENTLY
  1. IS = 2022-01-01 .. 2024-07-01  (a period every symbol has)
  2. OOS = 2024-07-01 .. 2026-10-01  (out of sample, after the IS)
  3. Symbol set restricted to those with data from the IS start — ARB excluded
     from the whole test for lacking history, rather than silently contributing
     a partial window
  4. Per-symbol trade counts printed, so a result driven by one symbol is visible
  5. Funding charged, adverse-first exits, next-bar fills, costs on — unchanged

Falsification:
  H1  the bear cross beats PF 1.0 in BOTH the honest IS and the OOS
  H2  its OOS return is positive after funding and costs
  H3  no single symbol accounts for the whole result (max symbol share < 60%)
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_engine import Costs, Decision, ReplayEngine

DB = Path(__file__).resolve().parents[1] / "data" / "trading.db"
# every symbol here has 4h data from 2022-01. ARB (2023-03) is excluded on purpose.
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
           "ADA/USDT", "LINK/USDT", "DOGE/USDT", "AVAX/USDT"]

IS_START, IS_END = "2022-01-01", "2024-07-01"
OOS_START, OOS_END = "2024-07-01", "2026-10-01"

COST = Costs(fee_bps=5.0, slippage_bps=5.0)
NOTIONAL = 5.0
MAX_OPEN = 8
FUNDING_PER_8H = 0.0001


def ms(d):
    return int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


def bearcross(bars):
    c = [b.close for b in bars]
    if len(c) < 202:
        return False
    af, bf = sma(c[:-1], 50), sma(c[:-1], 200)
    an, bn = sma(c, 50), sma(c, 200)
    if af is None or bf is None or an is None or bn is None:
        return False
    return af >= bf and an < bn


def run(a, b, stop, target, hold):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    eng = ReplayEngine(con, SYMBOLS, "4h", ms(a), ms(b), costs=COST,
                       notional=NOTIONAL, max_open=MAX_OPEN)
    def dec(feeds, openpos):
        out = []
        for sym, feed in feeds.items():
            if any(p.symbol == sym for p in openpos.values()):
                continue
            bb = feed.history(260)
            if len(bb) < 55:
                continue
            if bearcross(bb):
                out.append(Decision(sym, "enter_short", stop_frac=stop,
                                    target_frac=target, hold_bars=hold, tag="bc"))
        return out
    tr = eng.run(dec)
    if not tr:
        return None
    net = 0.0
    for t in tr:
        bh = max(0, (t.closed_ms - t.opened_ms) // (4 * 3600 * 1000))
        net += t.pnl - NOTIONAL * FUNDING_PER_8H * (bh / 6.0 * 3.0)
    n = len(tr)
    w = sum(1 for t in tr if t.pnl > 0)
    gw = sum(max(t.pnl, 0) for t in tr)
    gl = abs(sum(min(t.pnl, 0) for t in tr)) or 1e-9
    return {"n": n, "win": w / n, "pf": gw / gl, "net": net,
            "by_sym": Counter(t.symbol for t in tr)}


def hold(a, b):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rs = []
    for s in SYMBOLS:
        r = con.execute("select close from candles where symbol=? and timeframe='4h' "
                        "and open_time_utc>=? and open_time_utc<=? order by open_time_utc",
                        (s, ms(a), ms(b))).fetchall()
        if len(r) > 9:
            rs.append(r[-1][0] / r[0][0] - 1)
    return sum(rs) / len(rs) if rs else 0.0


def main():
    print("=== HONEST re-test: bear cross 50/200 SHORT ===")
    print(f"IS  {IS_START}..{IS_END}   (a window every symbol actually has)")
    print(f"OOS {OOS_START}..{OOS_END}")
    print(f"symbols {len(SYMBOLS)} (ARB excluded: no history before 2023-03)")
    print(f"cost 0.05%/side + funding {FUNDING_PER_8H*100:.3f}%/8h, adverse-first, next-bar fill\n")

    hi, ho = hold(IS_START, IS_END), hold(OOS_START, OOS_END)
    print(f"  BENCHMARK hold:  IS {hi*100:+.0f}%   OOS {ho*100:+.0f}%\n")

    print(f"  {'stop':>5} {'tgt':>6} {'hold':>5} | {'PFis':>5} {'PFoos':>6} "
          f"{'n_is':>5} {'n_oos':>6} {'netIS':>7} {'netOOS':>7} {'topSym':>8}")
    ok = []
    for stop, target, hold_b in [(0.04, 0.12, 540), (0.06, 0.18, 540),
                                 (0.08, 0.24, 540), (0.06, 0.12, 180),
                                 (0.04, 0.08, 180)]:
        r = run(IS_START, IS_END, stop, target, hold_b)
        o = run(OOS_START, OOS_END, stop, target, hold_b)
        if not r or not o:
            continue
        top = max(o["by_sym"].values()) / o["n"] if o["n"] else 1.0
        topname = o["by_sym"].most_common(1)[0][0] if o["by_sym"] else "-"
        mark = ""
        if r["pf"] > 1.0 and o["pf"] > 1.0 and o["net"] > 0 and top < 0.60:
            mark = "  <- PASSES"
            ok.append((stop, target, hold_b, r, o, top, topname))
        print(f"  {stop*100:4.0f}% {target*100:5.0f}% {hold_b:5} | {r['pf']:5.2f} "
              f"{o['pf']:6.2f} {r['n']:5} {o['n']:6} {r['net']:+7.2f} {o['net']:+7.2f} "
              f"{topname[:8]:>8}{mark}")

    print()
    if not ok:
        print("  VERDICT: nothing passes H1+H2+H3 on an honest window.")
        print("  The bear-cross short does not survive the corrected IS period.")
    else:
        print(f"  {len(ok)} config(s) pass H1+H2+H3.")
        for stop, target, hold_b, r, o, top, topname in ok:
            print(f"    sl{stop*100:.0f}% tp{target*100:.0f}% h{hold_b}: "
                  f"PF {r['pf']:.2f}/{o['pf']:.2f}  net ${r['net']:+.2f}/${o['net']:+.2f}  "
                  f"top symbol {topname} {top*100:.0f}%")
            print(f"    per-symbol OOS: {dict(o['by_sym'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
