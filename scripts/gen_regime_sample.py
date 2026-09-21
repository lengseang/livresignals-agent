#!/usr/bin/env python3
"""P2-01 sample: compute indicators + regime over live closed candles.

Reads from the trading.db, computes the indicator snapshot and regime for the
latest closed candle per symbol/timeframe, writes reports/p2/regime-sample.json.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from trading.config import SQLITE_PATH, SYMBOLS, TIMEFRAMES
from trading.signals.indicators import indicator_snapshot
from trading.signals.regime import regime_for_rows


def _rows_for(con: sqlite3.Connection, symbol: str, tf: str) -> list[dict]:
    cur = con.execute(
        "SELECT open,high,low,close,open_time_utc FROM candles "
        "WHERE symbol=? AND timeframe=? ORDER BY open_time_utc ASC",
        (symbol, tf),
    )
    return [dict(r) for r in cur.fetchall()]


def main() -> int:
    con = sqlite3.connect(str(SQLITE_PATH))
    con.row_factory = sqlite3.Row
    out: dict = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "symbols": {}}
    status = 0

    for symbol in SYMBOLS:
        out["symbols"][symbol] = {}
        for tf in TIMEFRAMES:
            rows = _rows_for(con, symbol, tf)
            if len(rows) < 60:
                out["symbols"][symbol][tf] = {"status": "insufficient", "n": len(rows)}
                status = 1
                continue
            snap = indicator_snapshot(rows)
            regime = regime_for_rows(rows)
            entry = {
                "n": len(rows),
                "last_open_utc": rows[-1]["open_time_utc"],
                "regime": regime,
                "indicators": {
                    k: round(v, 6) if v is not None else None
                    for k, v in snap.items()
                },
            }
            out["symbols"][symbol][tf] = entry
            print(f"{symbol} {tf}: regime={regime} adx14={snap['adx14'] and round(snap['adx14'],2)} ema20={snap['ema20'] and round(snap['ema20'],2)} close={snap['close']}")

    dest = Path(__file__).resolve().parents[1] / "reports" / "p2" / "regime-sample.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"report: {dest}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())