#!/usr/bin/env python3
"""P1-02 smoke: fetch BTC/USDT 1h candles via binance.vision, normalize, store."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from trading.config import SQLITE_PATH
from trading.data.ccxt_fetcher import fetch_closed_candles, make_exchange
from trading.data.normalize import detect_gaps, normalize_candle
from trading.data.store import init, storage_check, upsert_candle


def main() -> int:
    ex = make_exchange(sandbox=True)
    raw = fetch_closed_candles(ex, "BTC/USDT", "1h", limit=200)
    print(f"raw candles fetched: {len(raw)}")

    rows = []
    for r in raw:
        n = normalize_candle(r, "BTC/USDT", "1h")
        if n:
            rows.append(n)
    print(f"normalized closed candles: {len(rows)}")

    con = init(SQLITE_PATH)
    for r in rows:
        upsert_candle(con, r)
    con.commit()

    gaps = detect_gaps(rows)
    check = storage_check(con)
    print(json.dumps({
        "stored": len(rows),
        "duplicate_keys": check["duplicate_keys"],
        "gaps": gaps,
        "min_time_utc": min(r["open_time_utc"] for r in rows),
        "max_time_utc": max(r["open_time_utc"] for r in rows),
        "latest_close": rows[-1]["close"] if rows else None,
    }, indent=2))
    return 0 if rows and not gaps else 1


if __name__ == "__main__":
    sys.exit(main())