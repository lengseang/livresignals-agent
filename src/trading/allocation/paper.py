"""P-A paper wallet: virtual accumulation tracking, no exchange credentials.

Simulates monthly scaled entries against live/recorded prices and tracks a
virtual portfolio. This is the 'demo money' layer — zero credentials, fully
autonomous. When the user later provides testnet or real keys, the same
engine drives real orders; today it proves the cadence and produces reports.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from trading.allocation.engine import AllocationConfig, run_accumulation
from trading.config import SQLITE_PATH, REPORTS_DIR


class PaperWallet:
    """Virtual wallet: BTC + USDT, tracks entries, values at current price."""

    def __init__(self, starting_usdt: float = 10_000.0):
        self.usdt = starting_usdt
        self.btc = 0.0
        self.entries: list[dict] = []
        self.starting_usdt = starting_usdt

    def buy(self, usdt_amount: float, price: float) -> dict:
        if usdt_amount > self.usdt:
            usdt_amount = self.usdt
        btc_delta = usdt_amount / price
        self.btc += btc_delta
        self.usdt -= usdt_amount
        rec = {"ts_utc": datetime.now(timezone.utc).isoformat(), "price": price,
               "usdt": usdt_amount, "btc": btc_delta}
        self.entries.append(rec)
        return rec

    def value(self, price: float) -> dict:
        return {
            "btc": round(self.btc, 8),
            "usdt": round(self.usdt, 2),
            "btc_value_usdt": round(self.btc * price, 2),
            "total_usdt": round(self.usdt + self.btc * price, 2),
            "entries": len(self.entries),
        }


def build_paper_report(cfg: AllocationConfig | None = None) -> dict:
    """Run the accumulation strategy over the full 9yr history in paper mode.

    Uses the SAME engine as production (run_accumulation) — this validates the
    full-path cadence and produces a durable report. No credentials.
    """
    cfg = cfg or AllocationConfig()  # engine defaults = validated v2 rule
    con = sqlite3.connect(str(SQLITE_PATH))
    con.row_factory = sqlite3.Row
    cur = con.execute(
        "SELECT close FROM candles WHERE symbol='BTC/USDT' AND timeframe='1d' ORDER BY open_time_utc ASC"
    )
    closes = [float(r["close"]) for r in cur.fetchall()]
    state = run_accumulation(closes, cfg)
    final_price = closes[-1]
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "paper-virtual",
        "credentials": "none",
        "config": {
            "base": cfg.base_amount, "interval_d": cfg.interval_days,
            "lookback_d": cfg.lookback_days, "dd_thr": cfg.drawdown_threshold,
            "ext_thr": cfg.extension_threshold, "dip_x": cfg.dip_multiplier,
            "ext_x": cfg.extension_multiplier,
        },
        "result": {
            "btc_accumulated": round(state.btc, 8),
            "cash_invested_usdt": round(state.cash_invested, 2),
            "entries": state.entries,
            "final_price_usdt": final_price,
            "final_value_usdt": round(state.btc * final_price, 2),
            "return_pct": round((state.btc * final_price / state.cash_invested - 1) * 100, 2) if state.cash_invested else 0,
        },
    }
    dest = REPORTS_DIR / "p4-pa" / "paper-report.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    import sys
    r = build_paper_report()
    print(json.dumps(r, indent=2))
    sys.exit(0)