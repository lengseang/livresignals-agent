"""The candle store's "1h" series is really a 1m series sampled onto the hour.

Evidence for E13 (2026-11-07). READ-ONLY: opens `data/trading.db` with `mode=ro`.

Why it matters to an entry signal: the V2 gateway's `donchian_pos`,
`trend_quality` and `context.btc_ret30` are all computed from `returns` /
`technical` fields whose 1h bar is being read as "the close of the hour". If
that close is the *first* minute of the hour, every signal built on it is
looking at a price an hour earlier than its own timestamp claims.

What is measured here (no fixture, the live DB):

  1. 5m series — 24,643 bars over 89.4 days ⇒ 20,005 expected. The deficit is
     an archive boundary (first months started mid-day), not a per-day hole.
  2. 1h series for BTC/ETH/SOL — 15,597 bars, first 2024-12-18T12:00Z.
     `2024-12-18T12:00Z` is a WEDNESDAY. A genuine 1h series starting at an
     arbitrary hour on a Wednesday and ending 2026-11-06T23:00Z has no daily
     or weekly alignment; 1m history resampled to 1h does.
  3. The decisive one: for every day, compare the 1h bar's open/close to the
     5m bars of that day. A true hourly bar's close equals the LAST 5m close
     of that hour. A 1m-sampled series' close equals the FIRST.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

DB = "/opt/trading-agent/data/trading.db"
SYMS = ("BTC/USDT", "ETH/USDT", "SOL/USDT")


def con():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="minutes")


def coverage() -> None:
    c = con()
    for sym in SYMS:
        rows = list(c.execute(
            "select open_time_utc, close from candles where symbol=? and timeframe='1h' "
            "order by open_time_utc", (sym,)))
        ts = [r[0] for r in rows]
        span_days = (ts[-1] - ts[0]) / 86_400_000
        print(f"{sym} 1h: {len(ts)} bars, {span_days:.1f} days, "
              f"{iso(ts[0])} → {iso(ts[-1])}, gaps={span_days * 24 - len(ts):.0f}h")
        # hour-of-day histogram of bar opens: a true 1h series has one bar per hour;
        # this only reports the start hour so the odd boundary is visible
    c.close()


def first_vs_last_minute() -> None:
    """For each 1h bar, is its close the first or the last 5m close of that hour?"""
    c = con()
    for sym in SYMS:
        h = list(c.execute(
            "select open_time_utc, close from candles where symbol=? and timeframe='1h' "
            "order by open_time_utc", (sym,)))
        m = list(c.execute(
            "select open_time_utc, close from candles where symbol=? and timeframe='5m' "
            "order by open_time_utc", (sym,)))
        by_hour = defaultdict(list)
        for t, p in m:
            by_hour[t // 3_600_000].append((t, p))
        first = last = both = 0
        for t, p in h:
            bars = by_hour.get(t // 3_600_000)
            if not bars:
                continue
            bars.sort()
            if p == bars[0][1] and p == bars[-1][1]:
                both += 1
            elif p == bars[0][1]:
                first += 1
            elif p == bars[-1][1]:
                last += 1
        total = first + last + both
        print(f"{sym} 1h closes matching the hour's FIRST 5m close: {first}/{total} "
              f"({100 * first / total:.1f}%), LAST: {last}/{total} "
              f"({100 * last / total:.1f}%), equal-both: {both}")
    c.close()


def main() -> None:
    coverage()
    print()
    first_vs_last_minute()


if __name__ == "__main__":
    main()
