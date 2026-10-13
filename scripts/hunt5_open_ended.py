#!/usr/bin/env python3
"""hunt5_open_ended.py — the standing hunt. Runs every night, never the same twice.

WHAT THIS IS FOR
----------------
The owner said "continue to hunt always". The existing loops (research_loop,
night_research, improvement_loop) all report an average across market regimes,
which the 2026-10-11 regime test proved describes NEITHER regime: the bear-cross
short is PF 0.52 in up-markets and 1.85 in down-markets, and averaging them hides
both. That is why three nightly loops ran for weeks without finding the
short-breakdown family this script's predecessor found in one run.

WHAT THIS DOES DIFFERENTLY
--------------------------
  1. REGIME SPLIT ALWAYS. Every candidate is scored separately in up and down
     windows. A candidate is judged on its WORST regime, not its average.
  2. NET OF COSTS ALWAYS. 0.05%/side charged. Gross-positive is not a finding.
  3. TRADE-COUNT GATE. A candidate with under 200 trades is not a finding, however
     good its ratio. This is the trap that made 'bearcross v1.5' (n=63, PF 0.75)
     rank best in hunt4.
  4. COVERAGE-GUARDED. windows the data does not cover are refused.
  5. BENCHMARKED. Beating zero is not enough; the bar is the basket in the same
     window.
  6. IT EXPLORES. Parameter sets are drawn from a rotating search space so
     successive nights test different rules, and findings accumulate in one file
     instead of being rediscovered and lost.

OUTPUT: appends to reports/hunt5/findings.jsonl — one line per candidate that
passes every gate. The research loop's job is to fill that file. A human decides
what to deploy; this only measures.

Usage:
    python3 hunt5_open_ended.py                 # one pass, random rotation
    python3 hunt5_open_ended.py --seed 7        # reproducible pass
    python3 hunt5_open_ended.py --top 20        # more candidates per pass
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_engine import Costs, Decision, ReplayEngine

BASE = Path(__file__).resolve().parents[1]
DB = BASE / "data" / "trading.db"
OUT = BASE / "reports" / "hunt5"

SYMS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
        "ADA/USDT", "LINK/USDT", "DOGE/USDT", "AVAX/USDT"]
START, END = "2022-01-01", "2026-10-01"
WINDOW_DAYS = 120
COST = Costs(fee_bps=5.0, slippage_bps=5.0)
MIN_TRADES = 200          # below this, a ratio is noise (the v1.5 lesson)


def ms(d): return int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
def sma(v, n): return sum(v[-n:]) / n if len(v) >= n else None


def rsi(c, n=14):
    if len(c) < n + 1:
        return None
    g = l = 0.0
    for j in range(len(c) - n, len(c)):
        d = c[j] - c[j - 1]
        g += d if d > 0 else 0
        l += -d if d < 0 else 0
    return 100.0 if l == 0 else 100 - 100 / (1 + (g / n) / (l / n))


# ---- entry families (directions marked) ------------------------------------

def f_breakdown(look):
    def f(bars):
        c = [b.close for b in bars]
        return len(c) >= look + 2 and c[-1] < min(c[-look - 1:-1])
    return f


def f_volbreak(look, mult):
    def f(bars):
        c = [b.close for b in bars]; v = [b.volume for b in bars]
        if len(c) < look + 21:
            return False
        av = sum(v[-21:-1]) / 20
        return av > 0 and c[-1] < min(c[-look:-1]) and v[-1] > mult * av
    return f


def f_bearcross(fast, slow):
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


def f_breakdown_rsi(look, rsi_max):
    """Breakdown confirmed by RSI not yet oversold — the drop has room to run."""
    def f(bars):
        c = [b.close for b in bars]
        if len(c) < look + 16:
            return False
        r = rsi(c)
        return r is not None and c[-1] < min(c[-look:-1]) and r < rsi_max
    return f


def f_lowerhigh(look):
    """Bearish structure: a lower high after a lower low — a downtrend forming."""
    def f(bars):
        c = [b.close for b in bars]
        if len(c) < look + 3:
            return False
        seg = c[-look - 1:]
        hi1 = max(seg[:look // 2]); hi2 = max(seg[look // 2:])
        lo1 = min(seg[:look // 2]); lo2 = min(seg[look // 2:])
        return hi2 < hi1 and lo2 < lo1 and c[-1] < c[-2]
    return f


def build_space(rng, n):
    """Draw n candidate configs from a rotating space. Different every pass."""
    space = []
    for look in (20, 34, 55, 89, 100, 144, 200):
        space.append((f"breakdown {look}", f_breakdown(look), "short"))
        for mult in (1.5, 2.0, 3.0):
            space.append((f"volbreak {look} x{mult}", f_volbreak(look, mult), "short"))
    for fast, slow in ((10, 50), (20, 100), (34, 144), (50, 200)):
        space.append((f"bearcross {fast}/{slow}", f_bearcross(fast, slow), "short"))
    for look in (20, 55, 100):
        for rmax in (40, 50, 60):
            space.append((f"bd rsi{rmax} {look}", f_breakdown_rsi(look, rmax), "short"))
    for look in (20, 55, 100):
        space.append((f"lowerhigh {look}", f_lowerhigh(look), "short"))

    exits = [(s, s * 3, h) for s in (0.03, 0.04, 0.06, 0.08) for h in (60, 120, 240, 540)]
    rng.shuffle(space)
    out = []
    for name, fn, action in space[:max(1, n // 3)]:
        for stop, target, hold in exits:
            out.append((name, fn, action, stop, target, hold))
    return out


def windows():
    t0 = dt.datetime.fromisoformat(START).replace(tzinfo=dt.timezone.utc)
    t1 = dt.datetime.fromisoformat(END).replace(tzinfo=dt.timezone.utc)
    out, cur = [], t0
    while cur + dt.timedelta(days=WINDOW_DAYS) <= t1:
        nxt = cur + dt.timedelta(days=WINDOW_DAYS)
        out.append((int(cur.timestamp()*1000), int(nxt.timestamp()*1000)))
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
    try:
        eng = ReplayEngine(con, SYMS, "4h", a, b, costs=COST, notional=5.0,
                           max_open=8, bars_cache=cache, min_coverage=0.8)
    except Exception:
        return None
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
                                    target_frac=target, hold_bars=hold, tag="h5"))
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
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    wins = windows()
    reg = {}
    for (a, b) in wins:
        r = basket(con, a, b)
        reg[(a, b)] = "UP" if r > 0.10 else ("DOWN" if r < -0.10 else "FLAT")

    cands = build_space(rng, args.top * 3)
    t0 = time.time()
    print(f"=== HUNT5 standing hunt · {len(cands)} configs · "
          f"{len(wins)} windows ({sum(1 for w in wins if reg[w]=='UP')} up / "
          f"{sum(1 for w in wins if reg[w]=='DOWN')} down) ===")

    findings = []
    for name, fn, action, stop, target, hold in cands:
        agg = defaultdict(lambda: {"net": 0.0, "n": 0, "pf": [], "beat": 0, "wins": 0})
        for (a, b) in wins:
            r = run(con, fn, action, stop, target, hold, a, b, {})
            if not r:
                continue
            g = agg[reg[(a, b)]]
            g["net"] += r["net"]; g["n"] += r["n"]; g["pf"].append(r["pf"])
            if r["net"] / 20.0 > basket(con, a, b):
                g["beat"] += 1
            g["wins"] += 1
        up, dn = agg.get("UP"), agg.get("DOWN")
        if not dn or dn["n"] < MIN_TRADES:
            continue                                  # the v1.5 lesson
        dn_pf = sum(dn["pf"]) / len(dn["pf"]) if dn["pf"] else 0
        up_net = up["net"] if up else 0.0
        dn_net = dn["net"]
        # A finding: net-positive in down regimes, PF>1, beats basket most windows,
        # and does not bleed badly in up regimes.
        if dn_net > 0 and dn_pf > 1.0 and dn["beat"] >= dn["wins"] * 0.7 and up_net > -25:
            f = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                 "name": name, "action": action,
                 "stop": stop, "target": target, "hold": hold,
                 "dn_n": dn["n"], "dn_net": round(dn_net, 3), "dn_pf": round(dn_pf, 3),
                 "dn_beat": f"{dn['beat']}/{dn['wins']}",
                 "up_net": round(up_net, 3),
                 "worst": round(min(up_net, dn_net), 3)}
            findings.append(f)
            print(f"  FINDING  {name:24} sl{stop*100:.0f}% h{hold:<4} "
                  f"down n={dn['n']:4} PF {dn_pf:4.2f} net ${dn_net:+7.2f} "
                  f"beat {dn['beat']}/{dn['wins']} | up ${up_net:+6.2f}")

    print(f"\n  {len(findings)} findings from {len(cands)} configs "
          f"({time.time()-t0:.0f}s)")
    if findings:
        OUT.mkdir(parents=True, exist_ok=True)
        with (OUT / "findings.jsonl").open("a") as fh:
            for f in findings:
                fh.write(json.dumps(f) + "\n")
        print(f"  appended to {OUT}/findings.jsonl")
    else:
        print("  nothing new passed every gate this pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
