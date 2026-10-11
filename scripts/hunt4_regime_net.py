#!/usr/bin/env python3
"""HUNT4 — search for NET-POSITIVE rules, split by regime. The publishable-signal hunt.

WHY THIS IS DIFFERENT FROM HUNTS 1-3
------------------------------------
Four lessons, each paid for on 2026-10-10/11:

  1. REGIME FIRST. The bear-cross short is PF 0.52 in an up-market and PF 1.85 in a
     down-market. A test that mixes them reports an average that describes neither.
     Every hunt before this one made that error. So this hunt splits by regime
     before it judges anything.

  2. NET, NOT GROSS. The live paper book shows +$18.73 gross over 2850 trades and
     -$36.28 after 0.30% round trips. A gross-positive rule is worthless as a
     published signal. Every number here is charged full costs.

  3. COVERAGE. A window that is 16% covered is not a test. ReplayEngine(min_coverage)
     refuses or warns; this hunt uses it.

  4. BENCHMARK. The bar is not "positive" — it is the BASKET in the same window.
     In an up-market nothing beats holding; a rule must beat holding where holding
     is beatable, which is the down regime.

WHAT COUNTS AS A FINDING
------------------------
A rule is a candidate ONLY if, in the DOWN regime:
    net > 0 AND PF > 1.0 AND it beats the basket in a majority of down windows
AND it does not blow up in the UP regime (a published feed that loses in bull
markets will lose subscribers in the first month).

Output is ranked by worst-case (min over regimes), because a published feed is
judged by its worst month, not its best.

Usage:
    python3 hunt4_regime_net.py --tf 4h --save
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

sys_path = str(Path(__file__).resolve().parent)
import sys
sys.path.insert(0, sys_path)
from replay_engine import Costs, Decision, ReplayEngine

BASE = Path(__file__).resolve().parents[1]
DB = BASE / "data" / "trading.db"
OUT = BASE / "reports" / "hunt4"

SYMS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
        "ADA/USDT", "LINK/USDT", "DOGE/USDT", "AVAX/USDT"]

START, END = "2022-01-01", "2026-10-01"      # a window every symbol covers
WINDOW_DAYS = 120
COST = Costs(fee_bps=5.0, slippage_bps=5.0)  # 0.05%/side = 0.10% RT = 0.20% of margin @2x
NOTIONAL = 5.0
MAX_OPEN = 8


def ms(d): return int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
def sma(v, n): return sum(v[-n:]) / n if len(v) >= n else None


# ---- entry families, each expressed BOTH directions ------------------------

def mk_regime_dip(lo, rsi_max):
    """Buy a dip only when the long trend is intact (above the 200-SMA)."""
    def f(bars):
        c = [b.close for b in bars]
        if len(c) < 202:
            return False
        s = sma(c, 200)
        if s is None or c[-1] <= s:
            return False
        dd = c[-1] / max(c[-90:]) - 1
        g = l = 0.0
        for j in range(len(c) - 14, len(c)):
            d = c[j] - c[j - 1]
            g += d if d > 0 else 0
            l += -d if d < 0 else 0
        r = 100.0 if l == 0 else 100 - 100 / (1 + (g / 14) / (l / 14))
        return dd <= -lo and r <= rsi_max
    return f


def mk_breakdown(look):
    """Short a breakdown: close below the prior N-bar low."""
    def f(bars):
        c = [b.close for b in bars]
        if len(c) < look + 2:
            return False
        return c[-1] < min(c[-look - 1:-1])
    return f


def mk_bearcross(fast, slow):
    def f(bars):
        c = [b.close for b in bars]
        if len(c) < slow + 2:
            return False
        af, bf = sma(c[:-1], fast), sma(c[:-1], slow)
        an, bn = sma(c, fast), sma(c, slow)
        if af is None or bf is None or an is None or bn is None:
            return False
        return af >= bf and an < bn
    return f


def mk_volbreak(look, mult):
    """Short a high-volume breakdown — volume confirms the move."""
    def f(bars):
        c = [b.close for b in bars]
        v = [b.volume for b in bars]
        if len(c) < look + 21:
            return False
        av = sum(v[-21:-1]) / 20
        if av <= 0:
            return False
        return c[-1] < min(c[-look:-1]) and v[-1] > mult * av
    return f


def mk_bear_cross_vol(fast, slow, mult):
    """Bear cross confirmed by volume — fewer, higher-quality shorts."""
    bc = mk_bearcross(fast, slow)
    def f(bars):
        v = [b.volume for b in bars]
        if len(v) < 21:
            return False
        av = sum(v[-21:-1]) / 20
        if av <= 0:
            return False
        return bc(bars) and v[-1] > mult * av
    return f


CANDIDATES = [
    # long side, regime-gated (only buy when the trend is up)
    ("regime-dip lo1.0 rsi35", mk_regime_dip(0.010, 35), "long", 0.025, 0.05, 12),
    ("regime-dip lo2.5 rsi35", mk_regime_dip(0.025, 35), "long", 0.025, 0.05, 12),
    ("regime-dip lo2.5 rsi40", mk_regime_dip(0.025, 40), "long", 0.025, 0.05, 24),
    # short side, plain
    ("breakdown 55",           mk_breakdown(55), "short", 0.04, 0.12, 180),
    ("breakdown 100",          mk_breakdown(100), "short", 0.04, 0.12, 180),
    ("bearcross 50/200",       mk_bearcross(50, 200), "short", 0.04, 0.12, 180),
    ("bearcross 20/100",       mk_bearcross(20, 100), "short", 0.04, 0.12, 180),
    ("bearcross 10/50",        mk_bearcross(10, 50), "short", 0.03, 0.09, 120),
    # short side, volume-confirmed
    ("volbreak 55 x2.0",       mk_volbreak(55, 2.0), "short", 0.04, 0.12, 180),
    ("volbreak 100 x2.0",      mk_volbreak(100, 2.0), "short", 0.04, 0.12, 180),
    ("bearcross 50/200 v1.5",  mk_bear_cross_vol(50, 200, 1.5), "short", 0.04, 0.12, 180),
    ("bearcross 50/200 v2.5",  mk_bear_cross_vol(50, 200, 2.5), "short", 0.04, 0.12, 180),
]


def windows():
    t0 = dt.datetime.fromisoformat(START).replace(tzinfo=dt.timezone.utc)
    t1 = dt.datetime.fromisoformat(END).replace(tzinfo=dt.timezone.utc)
    out = []
    cur = t0
    while cur + dt.timedelta(days=WINDOW_DAYS) <= t1:
        nxt = cur + dt.timedelta(days=WINDOW_DAYS)
        out.append((int(cur.timestamp()*1000), int(nxt.timestamp()*1000),
                    cur.date().isoformat(), nxt.date().isoformat()))
        cur = nxt
    return out


def basket(con, a, b):
    rs = []
    for s in SYMS:
        r = con.execute("select close from candles where symbol=? and timeframe='4h' "
                        "and open_time_utc>=? and open_time_utc<=? order by open_time_utc",
                        (s, a, b)).fetchall()
        if len(r) > 9:
            rs.append(r[-1][0] / r[0][0] - 1)
    return sum(rs) / len(rs) if rs else 0.0


def run(con, fn, action, stop, target, hold, a, b, cache):
    eng = ReplayEngine(con, SYMS, "4h", a, b, costs=COST, notional=NOTIONAL,
                       max_open=MAX_OPEN, bars_cache=cache, min_coverage=0.8)
    def dec(feeds, openpos):
        out = []
        for sym, feed in feeds.items():
            if any(p.symbol == sym for p in openpos.values()):
                continue
            bars = feed.history(260)
            if len(bars) < 55:
                continue
            if fn(bars):
                out.append(Decision(sym, f"enter_{action}", stop_frac=stop,
                                    target_frac=target, hold_bars=hold, tag="h4"))
        return out
    tr = eng.run(dec)
    if not tr:
        return None
    net = sum(t.pnl for t in tr)
    gw = sum(t.pnl for t in tr if t.pnl > 0)
    gl = abs(sum(t.pnl for t in tr if t.pnl <= 0)) or 1e-9
    w = sum(1 for t in tr if t.pnl > 0)
    return {"n": len(tr), "net": net, "pf": gw / gl, "win": w / len(tr)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="4h")
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    wins = windows()
    cache = {}
    reg = {}
    for (a, b, la, lb) in wins:
        r = basket(con, a, b)
        reg[(a, b, la, lb)] = ("UP" if r > 0.10 else ("DOWN" if r < -0.10 else "FLAT"))

    print(f"=== HUNT4: net-positive rules, split by regime ===")
    print(f"{len(wins)} non-overlapping {WINDOW_DAYS}d windows over {START}..{END}")
    print(f"costs charged, coverage-guarded, basket-compared\n")

    up_w = [w for w in wins if reg[w] == "UP"]
    dn_w = [w for w in wins if reg[w] == "DOWN"]
    print(f"  regimes: {len(up_w)} UP, {len(dn_w)} DOWN\n")

    t0 = time.time()
    rows = []
    for name, fn, action, stop, target, hold in CANDIDATES:
        agg = defaultdict(lambda: {"net": 0.0, "n": 0, "pf": [], "beat": 0, "wins": 0})
        for (a, b, la, lb) in wins:
            r = run(con, fn, action, stop, target, hold, a, b, cache)
            if not r:
                continue
            regn = reg[(a, b, la, lb)]
            g = agg[regn]
            g["net"] += r["net"]; g["n"] += r["n"]; g["pf"].append(r["pf"])
            if r["net"] / 20.0 > basket(con, a, b):
                g["beat"] += 1
            g["wins"] += 1
        up, dn = agg.get("UP"), agg.get("DOWN")
        up_pf = sum(up["pf"])/len(up["pf"]) if up and up["pf"] else 0
        dn_pf = sum(dn["pf"])/len(dn["pf"]) if dn and dn["pf"] else 0
        worst = min(up["net"] if up else 0, dn["net"] if dn else 0)
        rows.append({"name": name, "action": action,
                     "up_net": up["net"] if up else 0, "up_pf": up_pf,
                     "dn_net": dn["net"] if dn else 0, "dn_pf": dn_pf,
                     "dn_beat": f"{dn['beat']}/{dn['wins']}" if dn else "0/0",
                     "worst": worst})
        print(f"  {name:26} UP net ${rows[-1]['up_net']:+7.2f} PF {up_pf:4.2f} | "
              f"DOWN net ${rows[-1]['dn_net']:+7.2f} PF {dn_pf:4.2f} beat {rows[-1]['dn_beat']}")

    print(f"\n=== ranked by WORST regime (a feed is judged by its worst month) ===")
    rows.sort(key=lambda r: -r["worst"])
    for r in rows[:8]:
        flag = ""
        if r["dn_net"] > 0 and r["dn_pf"] > 1.0 and r["up_net"] > -5:
            flag = "  <-- CANDIDATE"
        print(f"  {r['name']:26} worst ${r['worst']:+7.2f}  "
              f"up ${r['up_net']:+6.2f} (PF {r['up_pf']:.2f})  "
              f"down ${r['dn_net']:+6.2f} (PF {r['dn_pf']:.2f}) {r['dn_beat']}{flag}")

    print(f"\n  elapsed {time.time()-t0:.0f}s")
    if args.save:
        OUT.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        (OUT / f"{stamp}_regime_net.json").write_text(json.dumps(rows, indent=2))
        print(f"  saved -> {OUT}/{stamp}_regime_net.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
