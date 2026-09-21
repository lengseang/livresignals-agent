"""P-A testnet PnL tracker: computes profit/loss for paper positions.

Per-symbol: cost basis (from ledger entries), current value at live price,
unrealized PnL (USD and %). Portfolio totals. Writes reports/p5/pnl-report.json
and appends to an immutable PnL history CSV (append-only, for analytics).
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from trading.allocation.testnet_executor import LEDGER_FILE, SYMBOLS, load_ledger, save_ledger
from trading.config import REPORTS_DIR
from trading.data.ccxt_fetcher import make_exchange

PNL_HISTORY = REPORTS_DIR.parent / "data" / "pa-pnl-history.csv"


def fetch_prices() -> dict[str, float]:
    """Live testnet prices for the universe."""
    ex = make_exchange(sandbox=True)
    prices = {}
    for s in SYMBOLS:
        try:
            t = ex.fetch_ticker(s)
            prices[s] = float(t["last"])
        except Exception:  # noqa: BLE001
            pass
    return prices


def compute_pnl() -> dict:
    """Per-symbol + portfolio PnL from ledger at live prices."""
    ledger = load_ledger()
    prices = fetch_prices()
    symbols_detail = {}
    portfolio_cost = 0.0
    portfolio_value = 0.0
    realized_accumulated = 0.0  # no exits in accumulation; kept for future sells

    for sym in SYMBOLS:
        entries = [e for e in ledger["entries"] if e.get("symbol") == sym]
        if not entries:
            symbols_detail[sym] = {"position": 0.0, "cost_basis": 0.0, "value": 0.0, "pnl": 0.0, "pnl_pct": 0.0, "avg_entry": 0.0}
            continue
        qty = sum(float(e.get("qty") or e.get("qty_btc") or 0) for e in entries)
        cost = sum(float(e.get("amount_usdt") or 0) for e in entries)
        price = prices.get(sym)
        value = qty * price if price else cost
        pnl = value - cost
        pnl_pct = pnl / cost * 100 if cost > 0 else 0.0
        avg_entry = cost / qty if qty > 0 else 0.0
        symbols_detail[sym] = {
            "position": round(qty, 8),
            "cost_basis": round(cost, 2),
            "avg_entry": round(avg_entry, 4),
            "live_price": price,
            "value": round(value, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
        }
        portfolio_cost += cost
        portfolio_value += value

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "testnet-paper",
        "portfolio": {
            "cost_basis": round(portfolio_cost, 2),
            "market_value": round(portfolio_value, 2),
            "unrealized_pnl": round(portfolio_value - portfolio_cost, 2),
            "unrealized_pnl_pct": round((portfolio_value / portfolio_cost - 1) * 100, 2) if portfolio_cost > 0 else 0.0,
            "realized_pnl": round(realized_accumulated, 2),
            "total_pnl": round(portfolio_value - portfolio_cost + realized_accumulated, 2),
        },
        "symbols": symbols_detail,
    }
    dest = REPORTS_DIR / "p5" / "pnl-report.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def append_history(report: dict) -> None:
    """Append a snapshot row to the immutable PnL history CSV."""
    header = ["ts_utc", "portfolio_cost", "portfolio_value", "unrealized_pnl", "pnl_pct"]
    row = [
        report["generated_at_utc"],
        report["portfolio"]["cost_basis"],
        report["portfolio"]["market_value"],
        report["portfolio"]["unrealized_pnl"],
        report["portfolio"]["unrealized_pnl_pct"],
    ]
    new_file = not PNL_HISTORY.exists()
    PNL_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with open(PNL_HISTORY, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(header)
        w.writerow(row)


if __name__ == "__main__":
    report = compute_pnl()
    append_history(report)
    print(json.dumps(report, indent=2))