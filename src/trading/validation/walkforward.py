"""3-window walk-forward orchestration + gate report (P4)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from trading.config import SQLITE_PATH, SYMBOLS
from trading.validation.backtest import simulate
from trading.validation.metrics import (
    degradation,
    gate_passed,
    max_drawdown,
    profit_factor,
    sharpe_annualized,
    win_rate,
)

# Sample-size policy (P4-OD-01 proposal): >= 30 trades per test window.
MIN_TRADES = 30

N_WINDOWS = 3
# Window sizes adapt to timeframe: 1h uses 400/1200; 4h uses 100/300
# (each 4h candle = 4x the time span, so fewer candles cover the same history).
TRAIN_SIZE = 400
TEST_SIZE = 1200


def window_sizes(timeframe: str) -> tuple[int, int]:
    if timeframe == "4h":
        return 200, 500
    return TRAIN_SIZE, TEST_SIZE


def load_weights_override(path: str | None = None) -> dict | None:
    """Load an alternative weight table (R10 trend-following) or None."""
    if not path:
        return None
    import json

    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if all(r in data for r in ("RANGING", "TRENDING", "VOLATILE")):
            return data
    except (json.JSONDecodeError, KeyError):
        return None
    return None


def rows_for(con: sqlite3.Connection, symbol: str, timeframe: str = "1d", span: str = "recent") -> list[dict]:
    """Fetch rows for walk-forward.

    span:
      "recent"  -> the LAST N_WINDOWS*(train+test)+warmup candles (newest data;
                   truly untouched if prior gates ran on the oldest data)
      "earlier" -> the FIRST N_WINDOWS*(train+test)+warmup candles (oldest data)
      "all"     -> every candle (callers slice themselves)
    """
    cur = con.execute(
        "SELECT open,high,low,close,open_time_utc FROM candles "
        "WHERE symbol=? AND timeframe=? ORDER BY open_time_utc ASC",
        (symbol, timeframe),
    )
    rows = [dict(r) for r in cur.fetchall()]
    train_size, test_size = window_sizes(timeframe)
    need = N_WINDOWS * (train_size + test_size) + 60
    if span == "earlier":
        return rows[:need]
    if span == "recent":
        return rows[-need:] if len(rows) >= need else rows
    return rows


def window_metrics(trades: list[dict], equity_start: float) -> dict:
    rets = [t["ret"] for t in trades]
    equity_curve = [equity_start]
    for t in trades:
        equity_curve.append(equity_curve[-1] * (1 + t["ret"]))
    return {
        "sharpe": sharpe_annualized(rets),
        "max_drawdown": max_drawdown(equity_curve),
        "win_rate": win_rate(rets),
        "profit_factor": profit_factor(rets),
        "n_trades": len(rets),
    }


def run_walkforward(symbol: str, rows: list[dict], weights: dict | None = None, timeframe: str = "1d") -> dict:
    windows = []
    results = []
    n = len(rows)
    train_size, test_size = window_sizes(timeframe)
    need = N_WINDOWS * (train_size + test_size)
    if n < need:
        return {"symbol": symbol, "status": "insufficient_data", "rows": n, "needed": need, "windows": []}

    for w in range(N_WINDOWS):
        start = w * (train_size + test_size)
        train_rows = rows[start : start + train_size]
        test_rows = rows[start + train_size : start + train_size + test_size]
        windows.append({"train": [start, start + train_size], "test": [start + train_size, start + train_size + test_size]})

        train_sim = simulate(train_rows, weights=weights)
        # Test window uses train-tail warmup for features (point-in-time),
        # but trades only count inside the test region (no leakage).
        test_with_warmup = train_rows[-60:] + test_rows
        test_sim = simulate(test_with_warmup, trade_start=60, weights=weights)
        train_m = window_metrics(train_sim["trades"], 10_000.0)
        test_m = window_metrics(test_sim["trades"], 10_000.0)
        for m in (train_m, test_m):
            m["sharpe"] = round(m["sharpe"], 4)

        deg = {
            "sharpe": degradation(test_m["sharpe"], train_m["sharpe"]),
            "max_drawdown": degradation(test_m["max_drawdown"], train_m["max_drawdown"], is_drawdown=True),
            "win_rate": degradation(test_m["win_rate"], train_m["win_rate"]),
            "profit_factor": degradation(test_m["profit_factor"], train_m["profit_factor"]),
        }
        overall_deg = max(deg.values()) if deg else float("inf")
        test_m["degradation"] = overall_deg
        gate = gate_passed(test_m)
        results.append({
            "window": w + 1,
            "train": train_m,
            "test": test_m,
            "degradation_detail": deg,
            "gate": gate,
        })

    all_pass = all(r["gate"]["passed"] for r in results)
    sample_ok = all(r["test"]["n_trades"] >= MIN_TRADES for r in results)
    return {
        "symbol": symbol,
        "status": "PASS" if (all_pass and sample_ok) else "FAIL",
        "rows": n,
        "sample_size_policy": {"min_trades": MIN_TRADES, "satisfied": sample_ok},
        "windows": results,
    }


def main() -> int:
    import sys

    con = sqlite3.connect(str(SQLITE_PATH))
    con.row_factory = sqlite3.Row
    timeframe = sys.argv[1] if len(sys.argv) > 1 else "1d"
    span = sys.argv[2] if len(sys.argv) > 2 else "recent"
    weights_file = sys.argv[3] if len(sys.argv) > 3 else ""
    weights = load_weights_override(weights_file)
    gating = "TRENDING-only" if weights_file.endswith("gated") or False else ("custom" if weights else "default")

    # Regime gating applies only when NOT using custom weights (R10 test is ungated).
    effective_gating = "TRENDING-only" if (not weights and timeframe == "1h" and span == "earlier") else "none"
    from trading.validation.backtest import COST_PER_SIDE
    out: dict = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "timeframe": timeframe,
        "span": span,
        "weights_file": weights_file or "default",
        "regime_gating": effective_gating,
        "cost_model": {"taker_fee": 0.0010, "spread": 0.0002, "slippage": 0.0002, "round_trip_pct": round(COST_PER_SIDE * 200, 2)},
        "results": {},
    }
    for symbol in SYMBOLS:
        rows = rows_for(con, symbol, timeframe, span)
        out["results"][symbol] = run_walkforward(symbol, rows, weights=weights, timeframe=timeframe)
        r = out["results"][symbol]
        print(f"{symbol}: {r['status']} ({r.get('rows', '?')} rows, windows={len(r.get('windows', []))})")

    dest = Path(__file__).resolve().parents[3] / "reports" / "p4" / f"walkforward-gate-{timeframe}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"report: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())