#!/usr/bin/env python3
"""Ingest the book's alt universe into the candle store.

WHY THIS EXISTS
---------------
`ingest_all.py` iterates `config.SYMBOLS` = BTC/ETH/SOL × 5 timeframes = 15
series, and no alt ingester existed in any crontab. Measured 2026-10-25 08:00:
`data_harness.py` printed `candle store (sqlite) OK 0.1h 10 symbols` while 7 of
those 10 symbols had had no 5m bar for ~6h — and at 18:00 the same page reported
`stale 16h (worst ADA/ARB/AVAX/BN)` for the **whole alt column**. The store MAX
was fresh because BTC was fresh; every replay/learner reading the DB was
silently scoring yesterday.

This script only appends to `candles`. It writes no trading state.

DESIGN NOTES (each one is a bug that was measured on this store)
---------------------------------------------------------------
* Writes are `INSERT OR IGNORE`, so the FIRST write of a (symbol, tf, open) key
  wins forever. A bar still forming therefore can never be corrected later, so
  it must never be persisted — the same defect `scrape_history.py` had on 1d.
  `normalize_candle` already refuses an open window; we re-check explicitly.
* There is no gap detection here, and that is deliberate:
  `detect_gaps()` compares consecutive OPEN times inside one fetched page, so
  on a 200-bar forward fetch it reports a false "hole" for every legitimate
  non-trading minute on 4h/1d series and for the natural spacing of 30m bars in
  a 5m list. A real old hole needs an explicit backfill (`scrape_history.py`),
  not this ingester.
* Success is asserted PER SERIES (did this series' newest bar advance?), never
  by row count and never by `MAX(open_time)` over the table — those are the two
  aggregates that hid this outage.

Usage:
    python scripts/ingest_alts.py            # all ALTS, all timeframes
    python scripts/ingest_alts.py --dry-run  # report only, write nothing
Exit 0 only if every (symbol, timeframe) series advanced or was already current.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading.config import SQLITE_PATH, TIMEFRAMES
from trading.data.ccxt_fetcher import fetch_closed_candles, make_exchange
from trading.data.normalize import normalize_candle
from trading.data.store import init, upsert_candle

# The book trades 14 symbols (unified_trader.UNIVERSE); the store held 10.
# Keep this list to the alts the store already carries so the series keys do
# not change underneath `series_coverage`; NEAR/JUP/OP/SUI are still absent
# from the store entirely (see leng-trading-stack) and are out of scope here.
ALTS = ["XRP/USDT", "BNB/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT",
        "DOGE/USDT", "ARB/USDT"]

TF_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

# Ingest cadence is 15 min. A 5m series is "current" if its newest closed bar
# is within two slots + slack; anything older is dark and the run fails.
#
# The threshold must scale with the TIMEFRAME, and measuring that was the whole
# point: a flat 10-minute slack over a 4h bar flags every healthy series as
# STILL DARK (measured 2026-10-25 18:01 — 14 of 35 "failures" were 4h bars 6.0h
# old and 1d bars 42h old, i.e. exactly TWO slots behind on each, while the
# closed 5m/30m/1h bars had all arrived). A 1d bar is legitimately 42-66h old
# for most of every day, so a wall-clock age check cannot work here at all:
# only "how many closed slots are missing" can.
#
# Allowed missing slots per timeframe. 5m gets the most slack because its
# series is the monitor's own dark-feed probe; 4h/1d are allowed one pending
# slot (the exchange publishes a closed bar a few seconds after the boundary
# and the 15-min cron can land just before it).
MAX_MISSING_SLOTS = {"5m": 3, "15m": 2, "30m": 2, "1h": 1, "4h": 1, "1d": 1}


def _newest(con, sym: str, tf: str):
    row = con.execute(
        "SELECT MAX(open_time_utc) FROM candles WHERE symbol=? AND timeframe=?",
        (sym, tf)).fetchone()
    return row[0]


def main() -> int:
    dry = "--dry-run" in sys.argv
    ex = make_exchange(sandbox=True)
    con = init(SQLITE_PATH)

    total = 0
    failures = []
    for sym in ALTS:
        for tf in TIMEFRAMES:
            span = TF_MS[tf]
            # 15m included: at 15m the fetch limit IS the max self-heal window
            # (200 bars = 50h vs 60 bars = 15h). With limit=60 a 15m series dark
            # longer than 15h can never catch up from the forward fetch alone.
            limit = 200 if tf in ("5m", "15m", "30m", "1h") else 60
            before = _newest(con, sym, tf)
            try:
                raw = fetch_closed_candles(ex, sym, tf, limit=limit)
            except Exception as e:                       # noqa: BLE001
                failures.append(f"{sym}:{tf} fetch failed: {str(e)[:60]}")
                continue
            now_ms = int(time.time() * 1000)
            rows = []
            for r in raw:
                if int(r[0]) + span > now_ms:            # still forming
                    continue
                n = normalize_candle(r, sym, tf, now_ms=now_ms)
                if n:
                    rows.append(n)
            if not dry:
                for r in rows:
                    upsert_candle(con, r, now_ms=now_ms)
            after = max([r["open_time_utc"] for r in rows], default=before)
            total += len(rows)

            # per-SERIES verdict: did the newest closed slot arrive?
            expected = (now_ms // span) * span - span
            gap_slots = (expected - (after or 0)) // span
            if after is None or gap_slots > MAX_MISSING_SLOTS[tf]:
                failures.append(
                    f"{sym}:{tf} STILL DARK newest={after} "
                    f"({(now_ms - (after or 0)) / 3600e3:.1f}h, {gap_slots} slots behind)")

    if not dry:
        con.commit()
    n_series = len(ALTS) * len(TIMEFRAMES)
    print(f"{'DRY-RUN ' if dry else ''}upserted {total} bars across {n_series} series "
          f"({len(ALTS)} alts x {len(TIMEFRAMES)} tfs)")
    # Prove the check can FAIL: if the marker file data/ingest-alts-fail exists,
    # report one series dark even though it is not. Same rule as the lesson
    # loop's reject/pass pairs — a gate that cannot go red is not a gate, and
    # this one has already reported "OK" for 14 hours while the whole alt
    # column was frozen.
    if Path("/opt/trading-agent/data/ingest-alts-fail").exists():
        failures.append("SELFTEST: forced failure (data/ingest-alts-fail present)")
    if failures:
        print(f"FAILURES ({len(failures)}/{n_series}):")
        for f in failures:
            print("  " + f)
        con.close()
        return 1
    con.close()
    print(f"OK — every alt series current (newest closed slot per tf; "
          f"allowed missing {MAX_MISSING_SLOTS})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
