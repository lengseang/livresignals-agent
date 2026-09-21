#!/usr/bin/env python3
"""P4-R2: measure P2 score distribution per timeframe to calibrate entry.

Writes reports/p4/score-distribution.json.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from trading.config import SQLITE_PATH, SYMBOLS, TIMEFRAMES
from trading.signals.scorer import compute_score


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

    for symbol in SYMBOLS:
        out["symbols"][symbol] = {}
        for tf in TIMEFRAMES:
            rows = _rows_for(con, symbol, tf)
            scores = []
            regimes = Counter()
            for i in range(60, len(rows)):
                s = compute_score(rows[: i + 1])["score"]
                if s is not None:
                    scores.append(s)
                reg = compute_score(rows[: i + 1])["regime"]
                regimes[reg] += 1
            if not scores:
                out["symbols"][symbol][tf] = {"n_scores": 0}
                continue
            mean = sum(scores) / len(scores)
            pct = lambda p: sorted(scores)[min(len(scores) - 1, int(p / 100 * len(scores)))]
            entry_hit = sum(1 for s in scores if s >= 50) / len(scores)
            exit_hit = sum(1 for s in scores if s < 40) / len(scores)
            out["symbols"][symbol][tf] = {
                "n_scores": len(scores),
                "min": round(min(scores), 2),
                "max": round(max(scores), 2),
                "mean": round(mean, 2),
                "p25": round(pct(25), 2),
                "p50": round(pct(50), 2),
                "p75": round(pct(75), 2),
                "pct_score_ge_50": round(entry_hit, 3),
                "pct_score_lt_40": round(exit_hit, 3),
                "regimes": dict(regimes),
            }
            e = out["symbols"][symbol][tf]
            print(f"{symbol} {tf}: n={e['n_scores']} mean={e['mean']} p50={e['p50']} >=50:{e['pct_score_ge_50']:.0%} <40:{e['pct_score_lt_40']:.0%} regimes={e['regimes']}")

    dest = Path(__file__).resolve().parents[1] / "reports" / "p4" / "score-distribution.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"report: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())