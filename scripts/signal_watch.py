#!/usr/bin/env python3
"""Event-driven signal watcher + auto-trader.

Every 4h:
  1. Scan signals (dedup: only NEW state transitions fire)
  2. Broadcast each NEW signal card to #signals
  3. For tradeable BIG signals (dip_zone/deep_dip) → AUTO-TRADE on testnet
     (cadence + risk guards enforced by auto_trader)
  4. Trade confirmations → #alerts, daily report aggregates for accuracy
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading.signals import auto_trader, multi_signal  # noqa: E402
from trading.tg import bot_ui  # noqa: E402


def signal_card(sig: dict, trade_result: dict | None = None) -> tuple[str, str, list]:
    meta = multi_signal.SIGNAL_TYPES.get(sig["signal_type"], {})
    emoji = sig.get("emoji", "ℹ️")
    severity = sig.get("severity", "SMALL")
    trade_line = ""
    if trade_result:
        a = trade_result["action"]
        if a == "trade":
            trade_line = (f"\ntrade      : ✅ AUTO EXECUTED\n"
                          f"  order     : {trade_result.get('order_id')}\n"
                          f"  usdt      : {trade_result.get('amount_usdt'):.2f}\n"
                          f"  qty       : {trade_result.get('qty')}\n"
                          f"  mult      : x{trade_result.get('multiplier')}")
        elif a == "skip":
            trade_line = f"\ntrade      : ⏭ skipped — {trade_result.get('reason','')[:50]}"
    body = (
        "<pre>"
        f"type       : {sig.get('name', sig['signal_type'])}\n"
        f"severity   : {severity}\n"
        f"symbol     : {sig['symbol']}\n"
        f"price      : {sig.get('price', '?')}\n"
        f"drawdown%  : {sig.get('drawdown_pct', '?')}\n"
        f"ts_utc     : {sig['ts_utc'][:19]}Z\n"
        f"reason     : {sig.get('reason', '')}"
        f"{trade_line}"
        "</pre>"
    )
    sym = sig["symbol"]
    tv = "https://www.tradingview.com/chart/?symbol=BINANCE:" + sym.replace("/", "")
    buttons = [[{"text": "📈 Chart", "url": tv}]]
    prefix = "🔴" if severity == "BIG" else "🔵"
    return f"{prefix} {emoji} {sig['symbol']} — {sig.get('name', sig['signal_type'])}", body, buttons


def run() -> dict:
    new_signals = multi_signal.scan_all()
    results = []
    for sig in new_signals:
        # Auto-trade the tradeable BIG signals (paper money, guards enforced).
        trade_result = None
        if sig["signal_type"] in auto_trader.TRADEABLE_SIGNALS:
            trade_result = auto_trader.auto_trade_on_signal(sig)
            results.append({"symbol": sig["symbol"], "signal": sig["signal_type"],
                            "action": trade_result["action"]})
        title, body, buttons = signal_card(sig, trade_result)
        r = bot_ui.send_card("signals", title, body, buttons)
        if trade_result and trade_result["action"] == "trade":
            bot_ui.send_card(
                "alerts",
                f"✅ AUTO-TRADE {sig['symbol']}",
                "<pre>"
                f"signal     : {sig['signal_type']}\n"
                f"order      : {trade_result.get('order_id')}\n"
                f"usdt       : {trade_result.get('amount_usdt'):.2f}\n"
                f"qty        : {trade_result.get('qty')}\n"
                f"price      : {trade_result.get('price')}\n"
                f"multiplier : x{trade_result.get('multiplier')}"
                "</pre>",
                [[{"text": "📈 Chart",
                   "url": "https://www.tradingview.com/chart/?symbol=BINANCE:" + sig["symbol"].replace("/", "")}]],
            )
    return {"new_signals": len(new_signals), "results": results}


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
    if result["new_signals"] == 0:
        print("no new signals — state unchanged (dedup working)")