#!/usr/bin/env python3
"""One pass that REPAIRS the candles written before the store could refuse a
partial bar.

WHY THIS EXISTS
---------------
Until 2026-10-25 `upsert_candle` was INSERT OR IGNORE on (symbol, timeframe,
open_time_utc): the first write of a key won forever. Whenever the first write
was a snapshot of an in-progress candle, that partial OHLCV became the bar's
final truth. The readers were fixed to skip forming bars going forward
(`short_regime_paper.last_closed`, `scrape_history.rows_for_batch`), but the
rows already in the table are still wrong, and every reader must keep
remembering the rule.

WHAT COUNTS AS DAMAGED (measured, not guessed)
----------------------------------------------
A row is suspect when `received_time_utc - open_time_utc` is less than 10% of
its own window. A fetcher that runs every 15 minutes writes a 1h bar no earlier
than 15 minutes in (25% of the window), so <10% cannot come from a healthy
ingest. On 2026-10-25 01:00 UTC that selected:

    15m 210   30m 210   5m 63   1d 10   4h 10   1h 10

What is repaired is NOT inferred. The bar is re-fetched from the public mirror
and only replaced if the exchange returns the same open_time with a DIFFERENT
close/high/low/volume. If the exchange agrees with the stored row, the row was
captured late (a slow cron, a gap in the run) and is left alone. Nothing is
deleted; a dry run is the default.

WHAT IT DOES NOT DO
-------------------
No state file, no paper book, no order path, no config is written. Only the
`candles` rows listed above, and only with values the exchange returns today.

usage:
    repair_forming_bars.py                 # dry run, prints what would change
    repair_forming_bars.py --apply         # write the corrections
    repair_forming_bars.py --apply --tfs 15m,30m --limit 50
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading.config import SQLITE_PATH  # noqa: E402
from trading.data.normalize import _interval_ms, normalize_candle  # noqa: E402
from trading.data.store import connect  # noqa: E402

# A row captured in the first tenth of its window is a snapshot, not a bar.
SUSPECT_FRACTION = 0.10
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def suspect_rows(con: sqlite3.Connection, tfs: list[str]) -> list[sqlite3.Row]:
    out = []
    for tf in tfs:
        span = _interval_ms(tf)
        out.extend(con.execute(
            "SELECT symbol, timeframe, open_time_utc, close, high, low, volume, "
            "       received_time_utc "
            "FROM candles WHERE timeframe=? "
            "  AND received_time_utc - open_time_utc < ? "
            "ORDER BY symbol, open_time_utc",
            (tf, span * SUSPECT_FRACTION),
        ).fetchall())
    return out


def fetch_truth(ex, symbol: str, tf: str, open_ms: int, span_ms: int):
    """The exchange's own OHLCV for exactly this bar, or None if unavailable."""
    batch = ex.fetch_ohlcv(symbol, tf, since=open_ms, limit=3)
    for raw in batch:
        if int(raw[0]) == open_ms:
            return raw
    return None


def repair(con, rows, apply: bool, log=print) -> dict:
    from trading.data.ccxt_fetcher import make_exchange

    ex = make_exchange(sandbox=True)   # the same mirror the live ingest uses
    stats = {"checked": 0, "matched": 0, "repaired": 0, "unavailable": 0,
             "refused_write": 0}

    for r in rows:
        span = _interval_ms(r["timeframe"])
        try:
            raw = fetch_truth(ex, r["symbol"], r["timeframe"], r["open_time_utc"], span)
        except Exception as e:  # noqa: BLE001
            stats["unavailable"] += 1
            log(f"  {r['symbol']:10} {r['timeframe']:4} fetch error: {str(e)[:70]}")
            continue
        stats["checked"] += 1
        if raw is None:
            stats["unavailable"] += 1
            log(f"  {r['symbol']:10} {r['timeframe']:4} not returned by exchange")
            continue
        o, h, l, c, v = (float(x) for x in raw[1:6])
        if (c, h, l, v) == (r["close"], r["high"], r["low"], r["volume"]):
            stats["matched"] += 1
            continue                       # captured late, not damaged
        tag = "APPLY" if apply else "DRY  "
        log(f"  {tag} {r['symbol']:10} {r['timeframe']:4} "
            f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['open_time_utc'] / 1000))} "
            f"close {r['close']:.6g}->{c:.6g}  vol {r['volume']:.4g}->{v:.4g}")
        if not apply:
            stats["repaired"] += 1
            continue
        now_ms = int(time.time() * 1000)
        # Re-normalize through the same path an ingest uses, so the row carries
        # the same provenance fields. The bar has ended by construction (it is
        # in the table), so this returns a final row.
        row = normalize_candle(raw, r["symbol"], r["timeframe"], now_ms=now_ms)
        if row is None:
            stats["refused_write"] += 1
            continue
        from trading.data.store import upsert_candle
        # Force the repair regardless of the stored received_time: this row was
        # already judged partial by the empirical rule above. upsert_candle's
        # own condition (< close_time) is already true for a partial capture,
        # so a plain call is enough.
        if upsert_candle(con, row):
            stats["repaired"] += 1
        else:
            stats["refused_write"] += 1
        if stats["checked"] % 50 == 0:
            con.commit()
    if apply:
        con.commit()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the corrections")
    ap.add_argument("--tfs", default="5m,15m,30m,1h,4h,1d")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--db", default=str(SQLITE_PATH))
    a = ap.parse_args()

    con = connect(Path(a.db))
    tfs = [t for t in a.tfs.split(",") if t]
    rows = suspect_rows(con, tfs)
    if a.limit:
        rows = rows[: a.limit]
    per_tf = {}
    for r in rows:
        per_tf[r["timeframe"]] = per_tf.get(r["timeframe"], 0) + 1
    print(f"suspect rows (captured in first {SUSPECT_FRACTION:.0%} of their window): "
          f"{len(rows)}  {per_tf}")
    stats = repair(con, rows, a.apply)
    print(f"checked {stats['checked']} | matched-stored {stats['matched']} | "
          f"{'repaired' if a.apply else 'would repair'} {stats['repaired']} | "
          f"unavailable {stats['unavailable']} | refused {stats['refused_write']}")
    if not a.apply:
        print("dry run — nothing written. re-run with --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
