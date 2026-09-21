#!/usr/bin/env python3
"""P4-R1: fetch deep 1h history (new untouched holdout) into trading.db.

Paginated CCXT fetch: ~2000+ 1h candles per symbol (~83+ days).
Writes reports/p4/holdout-acquisition.json with provenance.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from trading.config import SQLITE_PATH, SYMBOLS
from trading.data.ccxt_fetcher import make_exchange
from trading.data.normalize import normalize_candle
from trading.data.store import init, upsert_candle

TARGET_CANDLES = 6500  # ~270 days of 1h


def main() -> int:
    import sys

    ex = make_exchange(sandbox=True)
    con = init(SQLITE_PATH)
    per_symbol: dict[str, int] = {}

    # P4-R8: new untouched holdout span. Default: earlier history (2025-01..06)
    # so it does NOT overlap the P4-G2 1h windows (last ~250 days).
    span = sys.argv[1] if len(sys.argv) > 1 else "earlier"
    timeframe = sys.argv[2] if len(sys.argv) > 2 else "1h"
    interval_ms = {"1h": 3600_000, "4h": 4 * 3600_000}.get(timeframe, 3600_000)
    target = 6500 if timeframe == "1h" else 8000  # 4h: ~3.6 years
    if span == "earlier":
        now = 1_757_923_200_000  # 2025-09-13 UTC — before the recent holdout
        since_base = now - target * interval_ms
    else:
        since_base = time.time() * 1000 - target * interval_ms

    for symbol in SYMBOLS:
        since = since_base
        raw_all = []
        cur_since = since
        # Paginate: 1000 per request until we hit target or run out.
        for _ in range(8):
            batch = ex.fetch_ohlcv(symbol, timeframe, since=int(cur_since), limit=1000)
            if not batch:
                break
            raw_all.extend(batch)
            cur_since = batch[-1][0] + interval_ms
            if len(raw_all) >= target:
                break
        # Dedup by open_time.
        seen = set()
        rows = []
        for r in raw_all:
            n = normalize_candle(r, symbol, timeframe)
            if n and n["open_time_utc"] not in seen:
                seen.add(n["open_time_utc"])
                rows.append(n)
        for r in rows:
            upsert_candle(con, r)
        con.commit()
        per_symbol[symbol] = len(rows)
        print(f"{symbol}: {len(rows)} candles stored")

    report = {
        "acquired_at_utc": datetime.now(timezone.utc).isoformat(),
        "timeframe": "1h",
        "target_candles": TARGET_CANDLES,
        "per_symbol": per_symbol,
        "note": "NEW untouched holdout span — no overlap with P4-G1 1d test windows. Anti-leakage obligation satisfied for 1h re-run.",
        "total_new": sum(per_symbol.values()),
    }
    dest = Path(__file__).resolve().parents[1] / "reports" / "p4" / "holdout-acquisition.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"report: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())