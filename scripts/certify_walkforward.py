#!/usr/bin/env python3
"""WALK-FORWARD certification for the short-breakdown rules.

This is the step the fee-stress table explicitly could NOT do: split the history
into CONSECUTIVE periods and require the rule to hold in every one of them.

WHY CONSECUTIVE, NEVER SHUFFLED
-------------------------------
Two mistakes made today both came from picking the configuration that looked best
on the sample being reported:

  1. "Drop the TIME exits" turned a losing family into PF 5.02 — pure lookahead,
     because whether a trade times out is not known at entry.
  2. The dip-buyer's "+0.125% edge" was the best of 14 hold hours on 40 trades;
     the repo's own out-of-sample tests had already measured -0.364% for the same
     entry.

So the folds here are consecutive in TIME and fixed in advance. A rule that is
positive in aggregate but negative in any fold is reported as regime-dependent
and is NOT certifiable. That is a stricter bar than the aggregate number, on
purpose: the aggregate is what a backtest wants you to look at.

The rules and their functions are imported from hunt5 rather than re-implemented,
so the certified rule is exactly the rule that was found.

Usage:
    python3 certify_walkforward.py                 # default 4 folds, 0.30% RT
    python3 certify_walkforward.py --folds 6 --fee 0.40
    python3 certify_walkforward.py --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

BASE = Path("/opt/trading-agent")
sys.path.insert(0, str(BASE / "scripts"))
sys.path.insert(0, str(BASE / "src"))

from replay_engine import ReplayEngine, Decision, Costs  # noqa: E402
import hunt5_open_ended as h5  # noqa: E402

DB = BASE / "data" / "trading.db"
TF = "4h"
EXIT = (0.04, 0.12, 120)      # stop 4%, target 12% (3R), hold 120 bars — the
                              # middle of the exit grid, fixed BEFORE looking at
                              # any fold result, so it cannot be tuned per fold

# The six distinct mechanisms the hunt found, at their best variants.
RULES = [
    ("bearcross 20/100",   h5.f_bearcross(20, 100)),
    ("volbreak 55 x3.0",   h5.f_volbreak(55, 3.0)),
    ("volbreak 20 x2.0",   h5.f_volbreak(20, 2.0)),
    ("volbreak 100 x1.5",  h5.f_volbreak(100, 1.5)),
    ("volbreak 55 x1.5",   h5.f_volbreak(55, 1.5)),
    ("volbreak 34 x1.5",   h5.f_volbreak(34, 1.5)),
]


def window(con: sqlite3.Connection) -> tuple[int, int]:
    """The window where EVERY symbol in SYMS actually has bars.

    Using min/max over all candles is wrong: BTC/ETH/SOL begin in 2022-01 while
    ARB begins 2023-03, so a nominal window starting at the earliest bar gives
    the early folds ~0% coverage for the majors — and a fold measured on almost
    no data is exactly the censoring that invents results. Take the LATEST start
    and the EARLIEST end across the symbols we will actually trade.
    """
    starts, ends = [], []
    for s in h5.SYMS:
        r = con.execute(
            "select min(open_time_utc), max(open_time_utc) from candles "
            "where symbol=? and timeframe=?", (s, TF)
        ).fetchone()
        if r and r[0] and r[1]:
            starts.append(int(r[0]))
            ends.append(int(r[1]))
    if not starts:
        raise RuntimeError("no candle data")
    return max(starts), min(ends)


def run_fold(con, fn, a, b, costs, cache):
    """One engine run over [a, b], grid-aligned so every feed starts together."""
    # Align both edges to the timeframe grid. The engine raises when feeds start
    # on different bars, which is the guard working — but a fold boundary that is
    # not on an exact multiple is what causes it, so align here rather than let
    # the run die.
    tf_ms = 4 * 3600 * 1000
    a = (a // tf_ms) * tf_ms
    b = (b // tf_ms) * tf_ms
    try:
        eng = ReplayEngine(con, h5.SYMS, TF, a, b, costs=costs, notional=5.0,
                           max_open=8, bars_cache=cache, min_coverage=0.8)
    except Exception as e:
        return {"error": str(e)[:80]}
    stop, target, hold = EXIT

    def dec(feeds, openpos):
        out = []
        for sym, feed in feeds.items():
            if any(p.symbol == sym for p in openpos.values()):
                continue
            bars = feed.history(260)
            if len(bars) < 55:
                continue
            if fn(bars):
                out.append(Decision(sym, "enter_short", stop_frac=stop,
                                    target_frac=target, hold_bars=hold, tag="wf"))
        return out

    tr = eng.run(dec)
    if not tr:
        return {"n": 0, "net": 0.0, "pf": None, "win": None}
    net = sum(t.pnl for t in tr)
    gw = sum(t.pnl for t in tr if t.pnl > 0)
    gl = abs(sum(t.pnl for t in tr if t.pnl <= 0)) or 1e-9
    return {"n": len(tr), "net": net, "pf": gw / gl,
            "win": sum(1 for t in tr if t.pnl > 0) / len(tr)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--fee", type=float, default=0.30, help="round trip, percent")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    lo, hi = window(con)
    span = hi - lo
    edges = [lo + span * i // args.folds for i in range(args.folds + 1)]
    costs = Costs(fee_bps=args.fee * 100 / 2, slippage_bps=0.0)
    cache: dict = {}

    results = {}
    for name, fn in RULES:
        folds = []
        for i in range(args.folds):
            folds.append(run_fold(con, fn, edges[i], edges[i + 1], costs, cache))
        nets = [f.get("net", 0.0) for f in folds]
        ns = [f.get("n", 0) for f in folds]
        total_n = sum(ns)
        total_net = sum(nets)
        pos = sum(1 for x in nets if x > 0)
        results[name] = {
            "folds": folds, "total_n": total_n, "total_net": total_net,
            "positive_folds": pos, "folds_n": args.folds,
            "all_positive": pos == args.folds and total_n >= 100,
            "min_fold_net": min(nets) if nets else 0.0,
        }

    if args.json:
        print(json.dumps({"timeframe": TF, "exit": EXIT, "folds": args.folds,
                          "fee_pct": args.fee, "rules": results}, indent=2))
        return 0

    print("=" * 90)
    print(f"WALK-FORWARD CERTIFICATION — {args.folds} consecutive folds, {TF}, "
          f"fee {args.fee:.2f}% RT")
    print(f"exit fixed in advance: stop {EXIT[0]*100:.0f}% / target "
          f"{EXIT[1]*100:.0f}% / hold {EXIT[2]} bars")
    print("=" * 90)
    print()
    hdr = f"{'rule':20} {'n':>5} {'net':>9} {'pos folds':>10}  per-fold net"
    print(hdr)
    print("-" * 90)
    for name, r in sorted(results.items(), key=lambda kv: -kv[1]["total_net"]):
        per = "  ".join(f"{f.get('net', 0):+7.1f}" for f in r["folds"])
        mark = "CERTIFIABLE" if r["all_positive"] else (
            "regime-dependent" if r["positive_folds"] else "FAILS")
        print(f"{name:20} {r['total_n']:5} {r['total_net']:+9.2f} "
              f"{r['positive_folds']}/{r['folds_n']:>8} {per}  {mark}")
    print("-" * 90)
    print()
    cert = [n for n, r in results.items() if r["all_positive"]]
    print(f"  certifiable (positive in EVERY fold, n>=100): {len(cert)}/{len(results)}")
    for n in cert:
        print(f"    ✅ {n}")
    print()
    print("  A rule positive overall but negative in one fold is NOT certified —")
    print("  that is precisely the regime-dependence that produced the -$24 up-regime")
    print("  bleed in the hunt. The aggregate number hides it; the folds do not.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
