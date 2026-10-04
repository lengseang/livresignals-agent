#!/usr/bin/env python3
"""Multi-timeframe history scraper — 5m through 1d, full available depth.

WHY THIS EXISTS
---------------
Every strategy test in this repo has been limited by sample size. The paper book
holds ~2,100 trades over weeks; real books hold ~50. That is far too little to
validate anything, which is why three plausible ideas died on the split-half
test tonight.

Binance serves 9 years of spot history and 7 years of futures history from a
public endpoint, no key required. Pulling it turns "wait months for data" into
"wait minutes". Lower timeframes matter most: 5m and 15m give hundreds of
thousands of bars per symbol, which is what a strategy book needs to be built on
evidence rather than hope.

WHAT IT DOES
------------
For each (symbol, timeframe), pages backwards through fetch_ohlcv and writes
rows into the existing candles table, which already has a UNIQUE index on
(symbol, timeframe, open_time_utc) — so re-running is safe and incremental by
construction. Nothing is duplicated, nothing overwritten.

Designed to be interruptible: it commits every page, so a kill or a crash leaves
the DB consistent and the next run resumes from where the data ends.

Read-only with respect to every trading state file. Writes only candles.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AGENT = Path("/opt/trading-agent")
DB = AGENT / "data/trading.db"

# 5m upward through the higher timeframes, as the owner asked. Order matters
# for perceived progress: the cheap big ones finish first and prove the path.
TIMEFRAMES = ["1d", "4h", "1h", "30m", "15m", "5m"]

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
    "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOGE/USDT", "ARB/USDT",
]

# Binance returns at most 1000 candles per call regardless of limit.
PAGE = 1000

# Rough per-symbol floor; the loop walks back until the exchange stops giving.
START = "2017-01-01T00:00:00Z"


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _existing_range(cur, symbol: str, tf: str):
    cur.execute(
        "SELECT MIN(open_time_utc), MAX(open_time_utc) FROM candles "
        "WHERE symbol=? AND timeframe=?",
        (symbol, tf),
    )
    return cur.fetchone()


def main() -> int:
    import ccxt

    only = None
    if "--tf" in sys.argv:
        only = sys.argv[sys.argv.index("--tf") + 1].split(",")

    tfs = [t for t in TIMEFRAMES if (only is None or t in only)]
    if not tfs:
        print("no timeframes selected")
        return 1

    # timeframe → milliseconds, for deriving close/event times from open time
    tf_ms = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000,
             "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

    ex = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    con = sqlite3.connect(str(DB), timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    cur = con.cursor()

    start = _ms(START)
    grand_total = 0

    for tf in tfs:
        span = tf_ms[tf]
        for sym in SYMBOLS:
            lo, hi = _existing_range(cur, sym, tf)
            # resume: if we already have data, start from just after the newest bar
            since = start
            if hi is not None:
                since = int(hi) + span
            got = 0
            t0 = time.time()
            while True:
                try:
                    batch = ex.fetch_ohlcv(sym, tf, since=since, limit=PAGE)
                except Exception as e:
                    print(f"  {sym:10} {tf:4} fetch error: {str(e)[:90]}  (stopping tf for this symbol)")
                    break
                if not batch:
                    break
                now_ms = int(time.time() * 1000)
                rows = []
                for o, op, h, l, c, v in batch:
                    rows.append((
                        sym, tf, int(o),
                        int(o) + span - 1,   # close_time_utc
                        op, h, l, c, v,
                        int(o) + span,       # event_time_utc
                        now_ms,              # received_time_utc
                        "backfill",          # source
                        f"binance:{sym}:{tf}:{int(o)}",   # source_id
                    ))
                cur.executemany(
                    "INSERT OR IGNORE INTO candles ("
                    "symbol, timeframe, open_time_utc, close_time_utc, "
                    "open, high, low, close, volume, "
                    "event_time_utc, received_time_utc, source, source_id"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows,
                )
                con.commit()                      # commit every page: interruptible
                got += len(batch)
                new_since = int(batch[-1][0]) + span
                if new_since <= since:            # no forward progress; stop
                    break
                since = new_since
                if len(batch) < PAGE:
                    break
                if time.time() - t0 > 900:        # per (sym,tf) time budget
                    break
            grand_total += got
            el = time.time() - t0
            print(f"  {sym:10} {tf:4} +{got:>7} bars  ({el:.0f}s)")

    con.close()
    print(f"\ntotal bars written: {grand_total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
