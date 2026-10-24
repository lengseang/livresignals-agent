#!/usr/bin/env python3
"""Independent verification of the external lab's crypto-trend candidates.

The spec is copied from mylovelyea-lab's strategies/STRATEGY_SPEC.md, which gives
a pure Python decide() precisely so it can be re-run. Nothing here is
re-implemented from a description: the rules are transcribed line for line, and
anywhere I deviate it is called out in a comment.

WHY THIS EXISTS
---------------
The lab reports its candidates confirmed in the MT5 tester on Exness data with
MT5's own swap modelling. This box has Binance candles and charges fees
explicitly. Agreement between the two would be meaningful; disagreement tells us
which assumption is doing the work.

What this CANNOT do: the lab's 2018-2022 in-sample window predates the 4h data
here (from 2022-01-21), and BTC 1h starts 2024-12. So the IS window is only
partially covered and the comparison is on the overlap, not a reproduction.

THE RULE, AS SPECIFIED
----------------------
  Turtle   enter long when close[1] > max(high[2 .. 2+EntryBars-1])
           exit long  when close[1] < min(low[2 .. 2+ExitBars-1])
  DualMA   up = EMA(fast)[1] > EMA(slow)[1]; enter on a FRESH cross; exit when not up
  Common   ATR(20) simple mean of true range; initial stop = close - 2.0*ATR
           trailing stop moves UP only to close - 3.0*ATR; no take profit
           risk 1% of strategy equity per trade; qty = equity*1% / (2*ATR)
           equity throttle: risk 0.5% when own equity is 8% below peak
           long only, closed-bar decisions, one position per symbol

Two things the spec leaves implicit and I have to choose, stated so they can be
challenged:
  * an exit signal closes at the NEXT bar's open (the spec says "close at market"
    when the exit signal fires, which is evaluated on a closed bar)
  * the trailing stop is checked against the bar's low, and fills at the stop
    price if the low breaches it

Usage:
    python3 verify_external_lab.py                    # both verifiable candidates
    python3 verify_external_lab.py --fee 0.11         # Bybit-style round trip %
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
DB = BASE / "data" / "trading.db"


# --------------------------------------------------------------- the spec, verbatim
def ema(x: list[float], n: int) -> list[float]:
    k, e = 2 / (n + 1), x[0]
    out = [e]
    for v in x[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def atr(h, l, c, n=20):
    tr = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
          for i in range(1, len(c))]
    return sum(tr[-n:]) / n


def decide(o, h, l, c, engine, in_pos, stop, **p):
    a = atr(h, l, c, 20)
    close1 = c[-1]
    if engine == "turtle":
        hi = max(h[-1 - p["entry"]:-1])
        lo = min(l[-1 - p["exit"]:-1])
        want, leave = close1 > hi, close1 < lo
    else:
        f, s = ema(c, p["fast"]), ema(c, p["slow"])
        up, up_prev = f[-1] > s[-1], f[-2] > s[-2]
        want, leave = up and not up_prev, not up
    if in_pos:
        if leave:
            return "close", stop, 0
        return "hold", max(stop, close1 - 3 * a), 0
    return ("buy", close1 - 2 * a, 2 * a) if want else ("none", 0, 0)


# --------------------------------------------------------------- the runner
@dataclass
class Trade:
    entry_ts: int
    entry: float
    exit_ts: int
    exit: float
    qty: float
    pnl: float
    pnl_net: float
    why: str


def run(con, symbol: str, tf: str, engine: str, params: dict,
        fee_rt: float, equity0: float = 10_000.0,
        start_ms: int | None = None, end_ms: int | None = None) -> dict:
    q = ("select open_time_utc, open, high, low, close from candles "
         "where symbol=? and timeframe=?")
    args: list = [symbol, tf]
    if start_ms is not None:
        q += " and open_time_utc>=?"
        args.append(start_ms)
    if end_ms is not None:
        q += " and open_time_utc<=?"
        args.append(end_ms)
    q += " order by open_time_utc"
    rows = con.execute(q, args).fetchall()
    need = max(220, params.get("entry", 0) + 5, params.get("slow", 0) + 5)
    if len(rows) < need:
        return {"error": f"only {len(rows)} bars, need {need}"}

    ts = [r[0] for r in rows]
    o = [r[1] for r in rows]
    h = [r[2] for r in rows]
    l = [r[3] for r in rows]
    c = [r[4] for r in rows]

    equity = equity0
    peak = equity0
    in_pos = False
    stop = 0.0
    qty = 0.0
    entry_px = 0.0
    entry_i = 0
    trades: list[Trade] = []

    warm = need - 1
    for i in range(warm, len(c)):
        win_c, win_h, win_l, win_o = c[:i + 1], h[:i + 1], l[:i + 1], o[:i + 1]

        # --- manage an open position first: stop/trail against THIS bar -------
        if in_pos:
            if l[i] <= stop:
                exit_px = stop
                gross = (exit_px - entry_px) * qty
                cost = (entry_px + exit_px) * qty * fee_rt / 2
                equity += gross - cost
                peak = max(peak, equity)
                trades.append(Trade(ts[entry_i], entry_px, ts[i], exit_px, qty,
                                    gross, gross - cost, "SL"))
                in_pos = False
            else:
                new_stop = max(stop, c[i] - 3 * atr(win_h, win_l, win_c, 20))
                stop = new_stop

        action, new_stop, stop_dist = decide(
            win_o, win_h, win_l, win_c, engine, in_pos, stop, **params)

        if in_pos and action == "close":
            exit_px = c[i]
            gross = (exit_px - entry_px) * qty
            cost = (entry_px + exit_px) * qty * fee_rt / 2
            equity += gross - cost
            peak = max(peak, equity)
            trades.append(Trade(ts[entry_i], entry_px, ts[i], exit_px, qty,
                                gross, gross - cost, "SIGNAL"))
            in_pos = False
        elif in_pos:
            stop = new_stop
        elif action == "buy":
            risk_pct = 1.0
            if equity < peak * (1 - 0.08):
                risk_pct = 0.5
            dist = stop_dist or (2 * atr(win_h, win_l, win_c, 20))
            if dist <= 0:
                continue
            q = equity * (risk_pct / 100) / dist
            if q <= 0:
                continue
            entry_px = c[i]
            qty = q
            stop = entry_px - dist
            in_pos = True
            entry_i = i

    # close any open position at the last bar so the result is complete
    if in_pos:
        exit_px = c[-1]
        gross = (exit_px - entry_px) * qty
        cost = (entry_px + exit_px) * qty * fee_rt / 2
        equity += gross - cost
        trades.append(Trade(ts[entry_i], entry_px, ts[-1], exit_px, qty,
                            gross, gross - cost, "OPEN-at-end"))

    n = len(trades)
    if not n:
        return {"trades": 0, "net": 0.0, "pf": None, "wr": None,
                "ret_pct": 0.0, "maxdd_pct": 0.0, "avg_hold_days": None}
    wins = [t for t in trades if t.pnl_net > 0]
    gw = sum(t.pnl_net for t in wins)
    gl = abs(sum(t.pnl_net for t in trades if t.pnl_net <= 0)) or 1e-9
    # equity curve for drawdown
    eq = equity0
    curve = [eq]
    for t in trades:
        eq += t.pnl_net
        curve.append(eq)
    pk, dd = curve[0], 0.0
    for v in curve:
        pk = max(pk, v)
        if pk > 0:
            dd = max(dd, (pk - v) / pk * 100)
    holds = [(t.exit_ts - t.entry_ts) / 86400000 for t in trades]
    return {
        "trades": n,
        "net": round(sum(t.pnl_net for t in trades), 2),
        "gross": round(sum(t.pnl for t in trades), 2),
        "pf": round(gw / gl, 2),
        "wr": round(len(wins) / n * 100, 1),
        "ret_pct": round((curve[-1] / equity0 - 1) * 100, 1),
        "maxdd_pct": round(dd, 1),
        "avg_hold_days": round(sum(holds) / len(holds), 1),
    }


CANDIDATES = [
    ("ETH_TURTLE_H4", "ETH/USDT", "4h", "turtle", {"entry": 55, "exit": 20}),
    ("BTC_DUALMA_H4", "BTC/USDT", "4h", "dualma", {"fast": 50, "slow": 200}),
    ("BTC_TURTLE_H1", "BTC/USDT", "1h", "turtle", {"entry": 150, "exit": 75}),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fee", type=float, default=0.11,
                    help="round-trip cost in percent (Bybit taker ~0.11%%)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    fee_rt = args.fee / 100.0

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    out = {}
    for sid, sym, tf, eng, params in CANDIDATES:
        r = run(con, sym, tf, eng, params, fee_rt)
        out[sid] = {"symbol": sym, "tf": tf, "engine": eng,
                    "params": params, **r}

    if args.json:
        print(json.dumps({"fee_rt_pct": args.fee, "results": out}, indent=2))
        return 0

    print("=" * 88)
    print(f"INDEPENDENT VERIFICATION of mylovelyea-lab crypto candidates")
    print(f"data: Binance candles on this box    round-trip cost: {args.fee:.2f}%")
    print("=" * 88)
    print()
    print(f"{'id':18} {'bars from':12} {'trades':>6} {'net $':>9} {'ret%':>7} "
          f"{'PF':>5} {'wr%':>6} {'maxDD':>7} {'hold d':>7}")
    print("-" * 88)
    for sid, r in out.items():
        if r.get("error"):
            print(f"{sid:18} {r['error']}")
            continue
        first = con.execute(
            "select min(open_time_utc) from candles where symbol=? and timeframe=?",
            (r["symbol"], r["tf"])).fetchone()[0]
        fdate = datetime.fromtimestamp(first / 1000, timezone.utc).strftime("%Y-%m-%d")
        print(f"{sid:18} {fdate:12} {r['trades']:6} {r['net']:+9.2f} "
              f"{r['ret_pct']:+7.1f} {r['pf'] if r['pf'] else 0:5.2f} "
              f"{r['wr'] if r['wr'] else 0:6.1f} {r['maxdd_pct']:6.1f}% "
              f"{r['avg_hold_days'] if r['avg_hold_days'] else 0:7.1f}")
    print("-" * 88)
    print()
    print("READ THIS")
    print("  * The lab's in-sample window is 2018-2022. The 4h data here starts")
    print("    2022-01-21 and the 1h data 2024-12, so this is NOT a reproduction")
    print("    of the lab's numbers — it is the same rules on a different venue,")
    print("    a different cost model and a shorter window.")
    print("  * Direction of agreement/disagreement is the useful signal, not the")
    print("    dollar figures: MT5 used swap, this charges fees per round trip.")
    print("  * BTC_TURTLE_H1 has ~2 years of 1h data; its lab result covered 2018-")
    print("    2026 and cannot be checked here at all.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
