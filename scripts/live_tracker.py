#!/usr/bin/env python3
"""Live BTC position tracker — shows the REAL Binance balance + P&L.

Your ~$10 real BTC position, tracked daily. This is the "skin in the game"
view: real money, real price, real value. Not a strategy — a live ledger.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def live_snapshot() -> dict:
    from trading.exchange.live_client import live_balances, make_live_exchange
    b = live_balances()
    ex = make_live_exchange()
    btc = b.get("BTC", 0)
    usdt = b.get("USDT", 0)
    ticker = ex.publicGetTickerPrice({"symbol": "BTCUSDT"})
    price = float(ticker["price"])
    btc_value = btc * price
    total = btc_value + usdt
    return {
        "btc_qty": btc,
        "btc_price": price,
        "btc_value_usdt": round(btc_value, 2),
        "usdt": usdt,
        "total_usdt": round(total, 2),
    }


if __name__ == "__main__":
    s = live_snapshot()
    print(json.dumps(s, indent=2))