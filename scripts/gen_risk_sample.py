#!/usr/bin/env python3
"""P3 sample: compute risk sizing for live P2 scores, writes reports/p3/risk-sample.json."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from trading.config import SQLITE_PATH
from trading.risk.breakers import check_portfolio_breakers, EquitySnapshot
from trading.risk.risk_engine import RiskRejected, compute_position
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
    out: dict = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "scenario": "10k equity, 2% stop, 1h signal", "positions": {}}

    risk_equity = 10_000.0
    for symbol in ("BTC/USDT", "ETH/USDT", "SOL/USDT"):
        rows = _rows_for(con, symbol, "1h")
        if len(rows) < 60:
            continue
        rec = decision_record(symbol, "1h", rows)
        score = rec["score"]
        entry = float(rows[-1]["close"])
        stop_distance = 0.02
        try:
            pos = compute_position(score=score, risk_equity=risk_equity, entry=entry, stop_distance=stop_distance)
            out["positions"][symbol] = {"score": score, "authorized": True, **{k: pos[k] for k in ("position_notional", "risk_amount", "risk_fraction", "stop_price")}}
        except RiskRejected as exc:
            out["positions"][symbol] = {"score": score, "authorized": False, "reason": str(exc)}
        print(f"{symbol}: score={score} -> {out['positions'][symbol].get('position_notional', 'REJECTED')}")

    # Breaker demo with today's snapshot.
    now = datetime.now(timezone.utc)
    snap = EquitySnapshot(utc=now, equity=9_950.0, realized_pnl=-50.0, unrealized_pnl=0.0, fees=1.0)
    bk = check_portfolio_breakers(day_start_equity=10_000.0, week_start_equity=10_000.0, high_water_mark=10_050.0, snapshot=snap)
    out["breaker_demo"] = {"halted": bk.halted, "reason": bk.reason}

    dest = Path(__file__).resolve().parents[1] / "reports" / "p3" / "risk-sample.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"report: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())