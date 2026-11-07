"""Reference-cell probe: an every-bar long, bucketed by UTC date.

Evidence for E13 (2026-11-07). READ-ONLY: `data/trading.db` opened `mode=ro`.

The V2 lanes have logged 18,742 decisions in both lanes and have never opened a
position (`heartbeat.json`: `positions: []`, `outcomes: ignored 1725`, exits all
zero). Before spending another research pass on improving an entry signal, it is
worth knowing what the *zero-skill* reference is on the same symbols over the
same window, because that is the number any entry rule must beat.

Method (declared before the numbers were read, per LOOP.md §2):
  * universe: BTC/USDT, ETH/USDT, SOL/USDT (the paper lanes' symbols)
  * window: whatever the 1h series holds — 2024-12-18T12:00Z → 2026-11-06T23:00Z
  * one position per UTC day, entered at that day's mean 1h close and held h days
  * net = gross − 0.1594% round trip (the measured fee basis in
    `expectancy_vs_fee.py`), no slippage. This is the OPTIMISTIC basis.

This is not a strategy proposal. It is the bar the strategy has to clear, and
it is deliberately the dumbest possible one.
"""
from __future__ import annotations

import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone

DB = "/opt/trading-agent/data/trading.db"
SYMS = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
FEE_RT = 0.001594


def daily_mean(sym: str) -> tuple[list, list]:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = list(c.execute(
            "select open_time_utc, close from candles where symbol=? and timeframe='1h' "
            "order by open_time_utc", (sym,)))
    finally:
        c.close()
    by_day: dict = defaultdict(list)
    for t, p in rows:
        by_day[datetime.fromtimestamp(t / 1000, tz=timezone.utc).date()].append(p)
    days = sorted(by_day)
    return days, [st.mean(by_day[d]) for d in days]


def hold_stats(prices: list, h: int) -> dict:
    r = [prices[i] / prices[i - h] - 1 for i in range(h, len(prices))]
    n = len(r)
    half = n // 2
    mean = st.mean(r)
    sd = st.stdev(r)
    return {"n": n, "gross": mean, "net": mean - FEE_RT,
            "t": mean / (sd / (n ** 0.5)) if sd else 0.0,
            "h1": st.mean(r[:half]), "h2": st.mean(r[half:]),
            "worst": min(r), "best": max(r)}


def main() -> None:
    for sym in SYMS:
        days, px = daily_mean(sym)
        total = px[-1] / px[0] - 1
        print(f"{sym}: {len(days)} days {days[0]} → {days[-1]}, "
              f"buy-and-hold over the window {total:+.1%}")
        for h in (7, 30):
            s = hold_stats(px, h)
            print(f"   hold {h:3d}d n={s['n']:4d} gross={s['gross']:+.2%} "
                  f"net={s['net']:+.2%} t={s['t']:+.1f} "
                  f"H1={s['h1']:+.2%} H2={s['h2']:+.2%} "
                  f"worst={s['worst']:+.0%} best={s['best']:+.0%}")
        print()


if __name__ == "__main__":
    main()
