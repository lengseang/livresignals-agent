#!/usr/bin/env python3
"""P-A walk-forward validation: scaled-DCA vs plain-DCA vs buy-and-hold.

Uses the UNTOUCHED recent span (1h data aggregated to daily where needed) and
3 rolling windows. The claim under test: drawdown scaling adds BTC over plain
DCA on equal capital, out-of-sample.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from trading.allocation.benchmark import buy_and_hold, compare_strategies, plain_dca
from trading.allocation.engine import AllocationConfig
from trading.config import SQLITE_PATH

N_WINDOWS = 3
WINDOW_DAYS = 365  # 1 year per window


def daily_closes_recent(con: sqlite3.Connection, symbol: str, n: int) -> list[float]:
    """Last n daily closes (truly untouched recent span)."""
    cur = con.execute(
        "SELECT close FROM candles WHERE symbol=? AND timeframe='1d' ORDER BY open_time_utc ASC",
        (symbol,),
    )
    closes = [float(r["close"]) for r in cur.fetchall()]
    return closes[-n:] if len(closes) >= n else closes


def run_pa_validation(symbol: str, closes: list[float]) -> dict:
    cfg = AllocationConfig(
        base_amount=100.0,
        interval_days=30,
        lookback_days=90,
        drawdown_threshold=-0.15,
        extension_threshold=0.15,
        dip_multiplier=2.0,
        extension_multiplier=0.5,
    )
    windows = []
    n = len(closes)
    win_len = WINDOW_DAYS
    need = N_WINDOWS * win_len
    if n < need:
        return {"symbol": symbol, "status": "insufficient_data", "rows": n, "needed": need, "windows": []}

    for w in range(N_WINDOWS):
        start = w * win_len
        span = closes[start : start + win_len]
        cmp = compare_strategies(span, cfg)
        bh = buy_and_hold(span)
        windows.append({
            "window": w + 1,
            "span": [start, start + win_len],
            "excess_btc_pct": round(cmp["excess_btc_pct"], 3),
            "scaled_value": round(cmp["scaled"]["value"], 2),
            "plain_value": round(cmp["plain"]["value"], 2),
            "buy_hold_value": round(bh["btc"] * span[-1], 2),
            "scaled_entries": cmp["entries"]["scaled"],
            "plain_entries": cmp["entries"]["plain"],
        })
        print(f"{symbol} W{w+1}: excess={windows[-1]['excess_btc_pct']}% scaled=${windows[-1]['scaled_value']:,.0f} plain=${windows[-1]['plain_value']:,.0f} bh=${windows[-1]['buy_hold_value']:,.0f}")

    all_excess_positive = all(w["excess_btc_pct"] > 0 for w in windows)
    return {
        "symbol": symbol,
        "status": "PASS" if all_excess_positive else "FAIL",
        "config": {
            "base": cfg.base_amount, "interval_d": cfg.interval_days,
            "lookback_d": cfg.lookback_days, "dd_thr": cfg.drawdown_threshold,
            "ext_thr": cfg.extension_threshold, "dip_x": cfg.dip_multiplier,
            "ext_x": cfg.extension_multiplier,
        },
        "windows": windows,
    }


def main() -> int:
    con = sqlite3.connect(str(SQLITE_PATH))
    con.row_factory = sqlite3.Row
    out: dict = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "3 rolling 365d windows on untouched recent 1d data; equal-total-capital comparison",
        "results": {},
    }
    # BTC 1d has 3,281 rows (2017-2026); recent 3yr = last ~1095.
    for symbol in ("BTC/USDT",):
        closes = daily_closes_recent(con, symbol, 1200)
        out["results"][symbol] = run_pa_validation(symbol, closes)

    dest = Path(__file__).resolve().parents[1] / "reports" / "p4-pa" / "walkforward-pa.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"report: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())