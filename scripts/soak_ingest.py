#!/usr/bin/env python3
"""P1-03 soak ingestion: fetch all symbols/timeframes, normalize, store.

Intended to run on a schedule (cron) during the 7-day P1 soak.
Exit code: 0 = ok, 1 = any symbol/timeframe fetch failure or gaps.
"""
from __future__ import annotations

import json
import sys
import time

from trading.config import SQLITE_PATH, SYMBOLS, TIMEFRAMES
from trading.data.ccxt_fetcher import fetch_closed_candles, make_exchange
from trading.data.health import check_data_health
from trading.data.normalize import detect_gaps, normalize_candle
from trading.data.store import init, record_error, storage_check, upsert_candle


def main() -> int:
    ex = make_exchange(sandbox=True)
    con = init(SQLITE_PATH)
    started = time.time()
    totals = {"fetched": 0, "stored": 0, "errors": 0}
    gaps_all = []

    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            try:
                raw = fetch_closed_candles(ex, symbol, tf, limit=500)
            except Exception as exc:  # noqa: BLE001 - record and continue
                record_error(con, "binance.vision", symbol, f"{tf}: {exc}")
                totals["errors"] += 1
                print(f"ERROR {symbol} {tf}: {exc}", file=sys.stderr)
                continue

            rows = []
            for r in raw:
                n = normalize_candle(r, symbol, tf)
                if n:
                    rows.append(n)
            for r in rows:
                upsert_candle(con, r)
            con.commit()

            gaps = detect_gaps(rows)
            if gaps:
                gaps_all.append({"symbol": symbol, "timeframe": tf, "gaps": gaps})
            totals["fetched"] += len(raw)
            totals["stored"] += len(rows)
            print(f"{symbol} {tf}: fetched={len(raw)} stored={len(rows)} gaps={len(gaps)}")

    elapsed = round(time.time() - started, 1)
    check = storage_check(con)
    health = check_data_health(SQLITE_PATH)

    report = {
        "ran_at_utc": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "elapsed_s": elapsed,
        "totals": totals,
        "duplicate_keys": check["duplicate_keys"],
        "gaps": gaps_all,
        "health": {"ok": health["ok"], "problems": health["problems"]},
    }
    out = SQLITE_PATH.parent.parent / "reports" / "phase1" / "soak-run.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))

    return 0 if (totals["errors"] == 0 and not gaps_all and health["ok"]) else 1


if __name__ == "__main__":
    sys.exit(main())