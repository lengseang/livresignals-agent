"""P-A live-forward paper tracker.

Advances the paper wallet as NEW daily candles arrive (cron-driven). Unlike
the historical replay (paper.py), this persists wallet state to JSON and only
processes candles the wallet has NOT seen yet — genuine forward tracking.

State file: data/pa-paper-state.json
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from trading.allocation.engine import AllocationConfig
from trading.config import SQLITE_PATH, PROJECT_ROOT

STATE_FILE = PROJECT_ROOT / "data" / "pa-paper-state.json"


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "last_processed_idx": -1,
        "btc": 0.0,
        "usdt_started": 10_000.0,
        "cash_invested": 0.0,
        "entries": [],
    }


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def advance(state: dict, cfg: AllocationConfig | None = None) -> dict:
    """Process any NEW daily candles since last run. Returns processed count."""
    cfg = cfg or AllocationConfig()
    con = sqlite3.connect(str(SQLITE_PATH))
    con.row_factory = sqlite3.Row
    cur = con.execute(
        "SELECT open_time_utc, close FROM candles "
        "WHERE symbol='BTC/USDT' AND timeframe='1d' ORDER BY open_time_utc ASC"
    )
    rows = [dict(r) for r in cur.fetchall()]
    closes = [float(r["close"]) for r in rows]
    con.close()

    last = state["last_processed_idx"]
    start = max(0, last + 1)
    processed = 0
    for idx in range(start, len(closes)):
        if idx < cfg.lookback_days:
            state["last_processed_idx"] = idx
            processed += 1
            continue
        # cadence check against last entry idx
        if state["entries"] and idx - state["entries"][-1]["idx"] < cfg.interval_days:
            state["last_processed_idx"] = idx
            processed += 1
            continue
        # scaling: buy 1.5x when price is >=15% below 90d high
        lookback = closes[idx - cfg.lookback_days : idx]
        hi = max(lookback)
        price = closes[idx]
        mult = cfg.dip_multiplier if (hi > 0 and price / hi - 1.0 <= cfg.drawdown_threshold) else 1.0
        amount = cfg.base_amount * mult
        if amount > state["usdt_started"] - state["cash_invested"]:
            amount = max(0.0, state["usdt_started"] - state["cash_invested"])
        if amount <= 0:
            # Wallet exhausted — mark processed, skip recording (no dead entries)
            state["last_processed_idx"] = idx
            processed += 1
            continue
        btc_delta = amount / price if price > 0 else 0.0
        state["btc"] += btc_delta
        state["cash_invested"] += amount
        state["entries"].append({
            "idx": idx, "price": price, "amount": amount,
            "multiplier": mult, "btc_delta": btc_delta,
            "ts_utc": datetime.utcnow().isoformat() + "Z",
        })
        state["last_processed_idx"] = idx
        processed += 1

    save_state(state)
    return {"processed": processed, "state": state}


def status() -> dict:
    state = load_state()
    con = sqlite3.connect(str(SQLITE_PATH))
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT close FROM candles WHERE symbol='BTC/USDT' AND timeframe='1d' "
        "ORDER BY open_time_utc DESC LIMIT 1"
    ).fetchone()
    con.close()
    price = float(row["close"]) if row else 0.0
    btc_value = state["btc"] * price
    return {
        "last_processed_idx": state["last_processed_idx"],
        "btc": round(state["btc"], 8),
        "cash_invested": round(state["cash_invested"], 2),
        "current_price": price,
        "portfolio_value_usdt": round(btc_value + (state["usdt_started"] - state["cash_invested"]), 2),
        "entries": len(state["entries"]),
        "state_file": str(STATE_FILE),
    }


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "advance"
    if mode == "advance":
        st = load_state()
        res = advance(st)
        print(json.dumps({"processed": res["processed"], **status()}, indent=2))
    else:
        print(json.dumps(status(), indent=2))