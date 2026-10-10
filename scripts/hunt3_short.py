#!/usr/bin/env python3
"""HUNT3 — the short-side search. The first hunt that does not compete with holding.

Why (see docs/proposals/short-side-hunt-plan-2026-10-10.md, written before this ran):

Every long-only search lost to buy-and-hold, because on assets that trend up over
years a timing rule pays fees to leave and re-enter a trend it would have kept by
sitting still. A SHORT profits on the falling leg, which buy-and-hold cannot
capture at all. If the market's asymmetry (down-runs max 16 bars vs 11 up) is
tradeable anywhere, it is here.

Falsification, pre-registered:
  H1  a short rule beats PF 1.0 in BOTH halves
  H2  it beats SHORT-AND-HOLD (the mirror benchmark), not just zero
  H3  it survives with funding charged (shorts pay every 8h)
  H4  >= 100 OOS trades

Usage:
    python3 hunt3_short.py --tf 4h
    python3 hunt3_short.py --tf 1d --save
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import time
from pathlib import Path

from replay_engine import Costs, Decision, ReplayEngine

DB = Path(__file__).resolve().parents[1] / "data" / "trading.db"
RESULTS = Path(__file__).resolve().parents[1] / "reports" / "hunt3"

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
           "ADA/USDT", "LINK/USDT", "DOGE/USDT", "AVAX/USDT", "ARB/USDT"]

IS_START, IS_END = "2017-01-01", "2023-01-01"
OOS_START, OOS_END = "2023-01-01", "2026-10-01"

COST = Costs(fee_bps=5.0, slippage_bps=5.0)     # futures taker 0.05% + slip, per side
NOTIONAL = 5.0
MAX_OPEN = 8
FREQ_CAP = 500

# Funding: shorts typically RECEIVE funding when it is positive (longs pay), but the
# sign flips in bear markets, and we do not have historical funding in the DB. So
# charge it as a COST to be conservative — a candidate must survive paying it.
# 0.01% per 8h = ~0.03%/day = ~11%/yr on the notional, a deliberately harsh charge.
FUNDING_PER_8H = 0.0001
BARS_PER_DAY = {"4h": 6.0, "1d": 1.0}


def ms(d: str) -> int:
    return int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def funding_cost(bars_held: int, tf: str, notional: float) -> float:
    """Funding paid over the hold, as an absolute cost on the notional."""
    days = bars_held / BARS_PER_DAY[tf]
    periods = days * 3.0                        # 8h periods per day
    return notional * FUNDING_PER_8H * periods


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


# --------------------------- short entry families ---------------------------
# Each is the MIRROR of a long family: sell strength / sell breakdown.

def mk_sell_rally(lo_n):
    """Close above the prior N-bar LOW is the mirror of a breakout.
    Here: price rallied into a band — sell the strength."""
    def f(bars):
        c = [b.close for b in bars]
        if len(c) < lo_n + 2:
            return False
        return c[-1] < max(c[-lo_n:]) and c[-1] > min(c[-lo_n:]) * 1.02
    return f


def mk_breakdown(look):
    """Close BELOW the prior N-bar low — the short mirror of the breakout.
    The most natural short: the trend is breaking down."""
    def f(bars):
        c = [b.close for b in bars]
        if len(c) < look + 2:
            return False
        return c[-1] < min(c[-look - 1:-1])
    return f


def mk_bear_cross(fast, slow):
    """Fast SMA crosses BELOW slow — the short mirror of the trend cross."""
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


def build_candidates():
    out = []
    for look in (20, 55, 100, 200):
        out.append({"fam": "breakdown", "name": f"breakdown {look}-bar low",
                    "mk": lambda look=look: mk_breakdown(look)})
    for fast, slow in [(10, 50), (20, 100), (50, 200)]:
        out.append({"fam": "xcross", "name": f"bear cross {fast}/{slow}",
                    "mk": lambda fast=fast, slow=slow: mk_bear_cross(fast, slow)})
    for lo in (20, 55):
        out.append({"fam": "rally", "name": f"sell-rally lo{lo}",
                    "mk": lambda lo=lo: mk_sell_rally(lo)})
    return out


def exits():
    for stop in (0.04, 0.06, 0.08, 0.12):
        for mult in (2.0, 3.0, 4.0):
            for hold in (60, 180, 540):
                yield stop, stop * mult, hold


def run_one(con, tf, entry_fn, stop, target, hold, start, end, cache=None):
    eng = ReplayEngine(con, SYMBOLS, tf, ms(start), ms(end), costs=COST,
                       notional=NOTIONAL, max_open=MAX_OPEN, bars_cache=cache)

    def decide(feeds, openpos):
        out = []
        for sym, feed in feeds.items():
            if any(p.symbol == sym for p in openpos.values()):
                continue
            bars = feed.history(260)
            if len(bars) < 55:
                continue
            if entry_fn(bars):
                out.append(Decision(sym, "enter_short", stop_frac=stop,
                                    target_frac=target, hold_bars=hold, tag="sh"))
        return out

    tr = eng.run(decide)
    if not tr:
        return None
    # charge funding on every trade
    net = 0.0
    for t in tr:
        bars_held = max(0, (t.closed_ms - t.opened_ms) // _bar_ms(tf))
        net += t.pnl - funding_cost(bars_held, tf, NOTIONAL)
    n = len(tr)
    w = sum(1 for t in tr if t.pnl > 0)
    gw = sum(max(t.pnl, 0) for t in tr)
    gl = abs(sum(min(t.pnl, 0) for t in tr)) or 1e-9
    years = (ms(end) - ms(start)) / (365.25 * 24 * 3600 * 1000)
    return {"n": n, "win": w / n, "net": net, "pf": gw / gl,
            "freq": n / years, "avg": net / n}


def _bar_ms(tf):
    return {"4h": 4 * 3600 * 1000, "1d": 24 * 3600 * 1000}[tf]


def short_hold(con, tf, start, end):
    """SHORT-AND-HOLD benchmark: short each symbol at the start, cover at the end.
    Equal weight. Negative of the basket return."""
    A, B = ms(start), ms(end)
    rets = []
    for s in SYMBOLS:
        rows = con.execute(
            "select close from candles where symbol=? and timeframe=? "
            "and open_time_utc>=? and open_time_utc<=? order by open_time_utc",
            (s, tf, A, B)).fetchall()
        if len(rows) < 10:
            continue
        rets.append(-(rows[-1][0] / rows[0][0] - 1))     # short = negative of long
    return sum(rets) / len(rets) if rets else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="4h")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cache: dict = {}
    cands = build_candidates()
    exs = list(exits())
    t0 = time.time()

    print(f"=== HUNT3 short-side search · tf {args.tf} ===")
    print(f"entries {len(cands)} x exits {len(exs)} = {len(cands)*len(exs)} configs")
    print(f"IS {IS_START}..{IS_END}   OOS {OOS_START}..{OOS_END}")
    print(f"cost 0.05%/side (futures taker) + funding {FUNDING_PER_8H*100:.3f}%/8h charged")
    print(f"GATE: PF>1 BOTH halves AND beat short-and-hold AND >=100 OOS trades\n")

    bench_is = short_hold(con, args.tf, IS_START, IS_END)
    bench_oos = short_hold(con, args.tf, OOS_START, OOS_END)
    print(f"  SHORT-AND-HOLD benchmark:  IS {bench_is*100:+.1f}%   OOS {bench_oos*100:+.1f}%")
    print(f"  (a short rule must BEAT this on return, not merely be positive)\n")

    survivors = []
    for c in cands:
        n_acc = 0
        f_is = p_is = 0.0
        for stop, target, hold in exs:
            r = run_one(con, args.tf, c["mk"](), stop, target, hold, IS_START, IS_END, cache)
            if not r or r["n"] < 50:
                continue
            f_is, p_is = r["freq"], r["pf"]
            if r["freq"] > FREQ_CAP or r["pf"] <= 1.0:
                continue
            o = run_one(con, args.tf, c["mk"](), stop, target, hold, OOS_START, OOS_END, cache)
            if not o or o["n"] < 100 or o["freq"] > FREQ_CAP or o["pf"] <= 1.0:
                continue
            # H2: beat short-and-hold on return
            ret_is = r["net"] / 20.0
            ret_oos = o["net"] / 20.0
            if ret_is <= bench_is or ret_oos <= bench_oos:
                continue
            survivors.append({"name": c["name"], "fam": c["fam"], "stop": stop,
                              "target": target, "hold": hold, "is": r, "oos": o,
                              "ret_is": ret_is, "ret_oos": ret_oos,
                              "combined_pf": (r["pf"] + o["pf"]) / 2})
            n_acc += 1
        print(f"    {c['name'][:38]:38} freq~{f_is:6.0f}/yr  PFis {p_is:.2f}  accepted {n_acc}")

    print(f"\n=== {len(survivors)} SURVIVORS (H1+H2+H3+H4 all pass) ===")
    if not survivors:
        print("  NONE. The short-side class is empty at this frequency too.")
        print("  Combined with the long-only results, the verdict is that no rule")
        print("  tested on this data beats simply holding the assets.")
    else:
        survivors.sort(key=lambda s: -s["combined_pf"])
        print(f"  {'config':30} {'PFis':>5} {'PFoos':>6} {'n_oos':>6} {'ret_is':>8} {'ret_oos':>8}")
        for s in survivors[:args.top]:
            print(f"  {s['name'][:30]:30} {s['is']['pf']:5.2f} {s['oos']['pf']:6.2f} "
                  f"{s['oos']['n']:6} {s['ret_is']*100:+7.1f}% {s['ret_oos']*100:+7.1f}%")
    print(f"\n  elapsed {time.time()-t0:.0f}s")

    if args.save:
        RESULTS.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        f = RESULTS / f"{stamp}_{args.tf}.json"
        f.write_text(json.dumps({
            "ran_at": stamp, "tf": args.tf, "mode": "short",
            "bench_short_hold": {"is": bench_is, "oos": bench_oos},
            "configs_tested": len(cands) * len(exs),
            "survivors": survivors,
            "verdict": "none" if not survivors else f"{len(survivors)} survive",
        }, indent=2))
        print(f"  saved -> {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
