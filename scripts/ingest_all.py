#!/usr/bin/env python3
"""Full market-data ingestion — all symbols × all timeframes.

Replaces the old BTC/1h-only ingest. Fetches every (symbol, timeframe) in
config.SYMBOLS × config.TIMEFRAMES from the public mirror, normalizes, and
upserts into the candles table. Idempotent (upsert, no dupes).

Exit 0 if every series advanced (or had no gaps); non-zero otherwise so PM2
and the health check can see a real failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading.config import SQLITE_PATH, SYMBOLS, TIMEFRAMES
from trading.data.ccxt_fetcher import fetch_closed_candles, make_exchange
from trading.data.normalize import detect_gaps, normalize_candle
from trading.data.store import init, upsert_candle


def main() -> int:
    ex = make_exchange(sandbox=True)
    con = init(SQLITE_PATH)

    total = 0
    problems = []
    per_series = {}

    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            limit = 200 if tf in ("5m", "30m", "1h") else 60
            try:
                raw = fetch_closed_candles(ex, sym, tf, limit=limit)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{sym}:{tf}: fetch failed: {str(e)[:80]}")
                continue
            rows = []
            for r in raw:
                n = normalize_candle(r, sym, tf)
                if n:
                    rows.append(n)
            for r in rows:
                upsert_candle(con, r)
            gaps = detect_gaps(rows)
            if gaps:
                problems.append(f"{sym}:{tf}: {len(gaps)} gaps")
            per_series[f"{sym}:{tf}"] = len(rows)
            total += len(rows)

    con.commit()
    con.close()

    print(f"ingested {total} candles across {len(per_series)} series")
    if problems:
        print("PROBLEMS:", "; ".join(problems))
        return 1
    print("OK — all series fresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
