"""P-A historical PnL replay: simulate the strategy on past data and record
the PnL curve as if the portfolio lived through it (multi-symbol).

Uses each symbol's own daily closes; for each month it invests per the
validated rule (1.5x on -15% drawdown, else 1.0x) and marks the portfolio
to market. Output: reports/p5/replay-pnl.json + CSV history.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from trading.allocation.engine import AllocationConfig
from trading.config import SQLITE_PATH, REPORTS_DIR

PNL_REPLAY = REPORTS_DIR.parent / "data" / "pa-replay-pnl.csv"
START_USDT = 10_000.0


def load_closes(con: sqlite3.Connection, symbol: str) -> list[float]:
    cur = con.execute(
        "SELECT close FROM candles WHERE symbol=? AND timeframe='1d' ORDER BY open_time_utc ASC",
        (symbol,),
    )
    return [float(r[0]) for r in cur.fetchall()]


def replay(symbols: list[str], cfg: AllocationConfig | None = None) -> dict:
    cfg = cfg or AllocationConfig()
    con = sqlite3.connect(str(SQLITE_PATH))
    positions = {s: 0.0 for s in symbols}
    cost_by_symbol = {s: 0.0 for s in symbols}
    # Align by the shortest symbol history; assume same dates (BTC longest).
    closes_map = {s: load_closes(con, s) for s in symbols}
    n = min(len(c) for c in closes_map.values())
    closes_map = {s: c[-n:] for s, c in closes_map.items()}  # align tail
    con.close()

    timeline = []
    investable = START_USDT
    for sym in symbols:
        last_entry_idx = {s: None for s in symbols}

    for idx in range(cfg.lookback_days, n):
        day_invested = 0.0
        for s in symbols:
            closes = closes_map[s]
            last = last_entry_idx[s]
            if last is not None and idx - last < cfg.interval_days:
                continue
            price = closes[idx]
            lookback = closes[idx - cfg.lookback_days : idx]
            hi = max(lookback)
            dd = price / hi - 1.0 if hi > 0 else 0.0
            mult = cfg.dip_multiplier if dd <= cfg.drawdown_threshold else 1.0
            amount = cfg.base_amount * mult
            if amount > investable:
                amount = max(0.0, investable)
            if amount <= 0:
                continue
            positions[s] += amount / price
            cost_by_symbol[s] += amount
            investable -= amount
            day_invested += amount
            last_entry_idx[s] = idx

        if day_invested > 0 or idx % 30 == 0:
            value = sum(positions[s] * closes_map[s][idx] for s in symbols)
            cost = sum(cost_by_symbol.values())
            pnl = value - cost
            timeline.append({
                "idx": idx,
                "date_utc": datetime.utcfromtimestamp(0).isoformat(),  # replaced below
                "invested": round(cost, 2),
                "value": round(value, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl / cost * 100, 2) if cost > 0 else 0.0,
            })

    final_value = sum(positions[s] * closes_map[s][-1] for s in symbols)
    final_cost = sum(cost_by_symbol.values())
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "start_capital": START_USDT,
        "final_cost": round(final_cost, 2),
        "final_value": round(final_value, 2),
        "total_pnl": round(final_value - final_cost, 2),
        "total_pnl_pct": round((final_value / final_cost - 1) * 100, 2) if final_cost > 0 else 0.0,
        "positions": {s: round(positions[s], 8) for s in symbols},
        "samples": timeline[-20:],  # tail for brevity
    }
    dest = REPORTS_DIR / "p5" / "replay-pnl.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Append-only CSV: per-symbol final stats
    new = not PNL_REPLAY.exists()
    with open(PNL_REPLAY, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["ts_utc", "symbol", "qty", "cost", "value", "pnl", "pnl_pct"])
        for s in symbols:
            qty = positions[s]
            cost = cost_by_symbol[s]
            value = qty * closes_map[s][-1]
            w.writerow([report["generated_at_utc"], s, round(qty, 8), round(cost, 2), round(value, 2), round(value - cost, 2), round((value / cost - 1) * 100, 2) if cost else 0.0])
    return report


if __name__ == "__main__":
    report = replay(["BTC/USDT", "ETH/USDT", "SOL/USDT"])
    print(json.dumps({k: v for k, v in report.items() if k != "samples"}, indent=2))