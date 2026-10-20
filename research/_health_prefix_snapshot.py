"""Pre-fix copy of src/trading/data/health.py, reconstructed 2026-10-20.

⚠️ THIS IS A RECONSTRUCTION, NOT A BYTE-EXACT SNAPSHOT. `src/trading/data/`
is gitignored (T0.7), the fix was applied in place, and the original bytes
were not preserved before editing. The three defects are reproduced exactly as
measured; the body is otherwise the current file with the fixes removed.

Use it only to re-run the new tests against the buggy behaviour:

    cp src/trading/data/health.py /tmp/health.fixed.py
    cp research/_health_prefix_snapshot.py src/trading/data/health.py
    PYTHONPATH=src venv/bin/python -m pytest tests/test_data_health_report.py -q
    cp /tmp/health.fixed.py src/trading/data/health.py   # restore

Expected against this file: the publish, storage and ok_all tests fail.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from trading.config import FRESHNESS_DEADLINES, REPORTS_DIR, SYMBOLS, TIMEFRAMES, SQLITE_PATH


def check_data_health(db_path: Path | None = None) -> dict:
    con = sqlite3.connect(str(db_path or SQLITE_PATH))
    con.row_factory = sqlite3.Row
    now_ms = int(time.time() * 1000)
    problems = []
    detail = {}

    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            row = con.execute(
                "SELECT MAX(close_time_utc) AS max_close, MAX(open_time_utc) AS latest, COUNT(*) AS n "
                "FROM candles WHERE symbol=? AND timeframe=?",
                (symbol, tf),
            ).fetchone()
            latest = row["latest"]
            max_close = row["max_close"]
            n = row["n"]
            key = f"{symbol}:{tf}"
            if latest is None:
                problems.append(f"{key}: no data")
                detail[key] = {"n": 0, "fresh": False, "gap": None}
                continue
            deadline_ms = FRESHNESS_DEADLINES[tf] * 1000
            fresh = (now_ms - max_close) <= deadline_ms
            if not fresh:
                problems.append(f"{key}: stale (max_close {max_close} now {now_ms})")
            gap = None
            expected_next_open = max_close + 1
            if latest > expected_next_open:
                gap = {"expected": expected_next_open, "actual": latest}
                problems.append(f"{key}: gap detected {gap}")
            detail[key] = {"n": n, "fresh": fresh, "latest_open": latest,
                           "max_close": max_close, "gap": gap}

    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "ok": len(problems) == 0,
        "problems": problems,
        "detail": detail,
        "symbols": SYMBOLS,
        "timeframes": TIMEFRAMES,
    }

    # DEFECT 1: published by every caller, including tests on a temp DB.
    out = REPORTS_DIR / "data-health.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    con.close()
    return report


def main() -> int:
    report = check_data_health()
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
