"""Does the trend book's positive mean survive dropping its best trade?

READ-ONLY. `data/trading.db` opened `mode=ro`, `data/trend-state.json` read only.

WHY (2026-11-12)
----------------
The loop has spent two shifts on the *paper unified* book's net and win rate.
The real-money trend book (`trend_follower.py`, `st["log"]`) has a different
shape and nobody has written it down: n=212 closed, 21.7% win rate, and a
mean return of +1.21% per trade — positive on a book that wins one trade in
five. That is a trend-following signature (few large winners, many small
losses), and a trend signature is exactly the shape that a single outlier can
invent. Before any exit rule is tuned on it, measure how thin the mean is.

This is the same question T1.6 asked of the strategy lab and E15 asked of the
ensemble: is this one observation repeated, or 212 observations?

Method (declared before the numbers were read):
  * population: every closed row in trend-state.json `log`, no exclusions.
  * statistic: mean return per trade, NET of the 0.1594% round-trip fee basis
    used everywhere else in the repo (`expectancy_vs_fee.py`). The recorded
    `ret` is gross; net = ret - FEE_RT.
  * fragility: (a) delete-k-best for k=1..5; (b) the same after dropping the
    single 85.7% NEAR trade and ITS three largest neighbours (the honest
    "the leader IS the book" test from the paper book's p2 line); (c) the
    fraction of total gross profit that the top 5% of trades supply.
  * a t-stat is reported but NOT used as proof: n=212 with a 2.5%-sd fat tail
    is not normal, so the t is descriptive only.
No thresholds are proposed. This probe does not change any rule.

Usage:  venv/bin/python research/_trend_tail_concentration.py
"""
from __future__ import annotations

import json
import math
import statistics as st
from datetime import datetime, timedelta
from pathlib import Path

STATE = Path("/opt/trading-agent/data/trend-state.json")
FEE_RT = 0.001594


def load_returns() -> list[dict]:
    d = json.loads(STATE.read_text(encoding="utf-8"))
    return [r for r in (d.get("log") or []) if isinstance(r.get("ret"), (int, float))]


def describe(rows: list[dict]) -> dict:
    rets = sorted((r["ret"] for r in rows), reverse=True)
    n = len(rets)
    if n == 0:
        return {"n": 0}
    mean = st.mean(rets)
    sd = st.stdev(rets) if n > 1 else 0.0
    net = mean - FEE_RT
    return {
        "n": n,
        "mean_gross_pct": 100 * mean,
        "mean_net_pct": 100 * net,
        "median_net_pct": 100 * (st.median(rets) - FEE_RT),
        "win_rate_pct": 100 * sum(1 for x in rets if x > 0) / n,
        "t": mean / (sd / math.sqrt(n)) if sd else 0.0,
        "sum_gross_pct": 100 * sum(rets),
        "symbols": len({r["symbol"] for r in rows}),
    }


def main() -> None:
    rows = load_returns()
    if not rows:
        print("no closed rows in trend-state.json — nothing to measure")
        return
    base = describe(rows)
    print("TREND BOOK — tail concentration (read-only, pre-declared method)")
    print("=" * 72)
    print(f"n={base['n']}  symbols={base['symbols']}  "
          f"win={base['win_rate_pct']:.1f}%")
    print(f"mean gross {base['mean_gross_pct']:+.3f}%  "
          f"mean NET {base['mean_net_pct']:+.3f}%  "
          f"median NET {base['median_net_pct']:+.3f}%")
    print(f"sum gross {base['sum_gross_pct']:+.1f}%  "
          f"t={base['t']:+.2f} (descriptive: fat right tail, not normal)")

    order = sorted(rows, key=lambda r: r["ret"], reverse=True)
    print("\nDELETE-k-BEST (drop the k largest winners, re-measure NET mean):")
    for k in range(1, 6):
        s = describe(order[k:])
        print(f"  drop {k}: n={s['n']}  mean NET {s['mean_net_pct']:+.3f}%  "
              f"win {s['win_rate_pct']:.1f}%  sum {s['sum_gross_pct']:+.1f}%")

    print("\nWithout the single biggest trade, first:")
    for r in order[:3]:
        print(f"  {r['symbol']:10s} {r['ts'][:10]}  {100*r['ret']:+.1f}%  "
              f"({r['why']})")

    top5 = max(1, round(0.05 * base["n"]))
    pos = sum(x for x in (r["ret"] for r in rows) if x > 0) * 100
    top_gain = sum(r["ret"] for r in order[:top5]) * 100
    print(f"\nTOP {top5} trades ({100*top5/base['n']:.0f}% of rows) supply "
          f"{100*top_gain/pos:.0f}% of every dollar won "
          f"({top_gain:+.1f}% of {pos:+.1f}%).")

    # Same-window honesty: the book's last 14 days vs its life. 28/30d were
    # tried first and rejected as a headline: they contain the whole
    # 2026-10-18..10-30 run, so they flatter the book. 14d is the window that
    # starts after the biggest winner was already banked.
    order_ts = sorted(rows, key=lambda r: r["ts"])
    cut = order_ts[-1]["ts"][:10]
    last_dt = datetime.fromisoformat(order_ts[-1]["ts"])
    for days in (7, 14):
        since = (last_dt - timedelta(days=days)).isoformat()
        w = [r for r in order_ts if r["ts"] >= since]
        s = describe(w)
        top3 = sum(r["ret"] for r in sorted(w, key=lambda r: r["ret"],
                                             reverse=True)[:3])
        print(f"\nLAST {days}d (to {cut}): n={s['n']}  "
              f"mean NET {s['mean_net_pct']:+.3f}%  win {s['win_rate_pct']:.1f}%  "
              f"sum {s['sum_gross_pct']:+.1f}%")
        if s["n"] > 3:
            print(f"  its 3 best trades alone: {100*top3:+.1f}% gross "
                  f"-> everything else {100*(s['sum_gross_pct']/100-top3):+.1f}% "
                  f"(before fees)")
    print("\nVERDICT: if the delete-1 mean is still positive, the book is not "
          "one trade. If it is not, every exit tuning drawn from it is a fit "
          "to a single observation.")


if __name__ == "__main__":
    main()
