#!/usr/bin/env python3
"""P2-02 sample: scores over live closed candles, writes reports/p2/scorer-sample.json."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from trading.config import SQLITE_PATH, SYMBOLS, TIMEFRAMES
from trading.signals.scorer import decision_record


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
    out: dict = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "decisions": {}}
    status = 0

    for symbol in SYMBOLS:
        out["decisions"][symbol] = {}
        for tf in TIMEFRAMES:
            rows = _rows_for(con, symbol, tf)
            if len(rows) < 60:
                out["decisions"][symbol][tf] = {"score": None, "regime": "RANGING", "missing": ["insufficient_rows"], "n": len(rows)}
                status = 1
                continue
            rec = decision_record(symbol, tf, rows)
            out["decisions"][symbol][tf] = {
                "score": rec["score"],
                "regime": rec["regime"],
                "missing": rec["missing"],
                "action": rec["action"],
                "n": len(rows),
            }
            print(f"{symbol} {tf}: score={rec['score']} regime={rec['regime']} action={rec['action']}")

    dest = Path(__file__).resolve().parents[1] / "reports" / "p2" / "scorer-sample.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"report: {dest}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())