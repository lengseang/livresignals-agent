#!/usr/bin/env python3
"""Button-triggered trade — routes through the FULL guard stack.

Called by the ctrade: Telegram button. Unlike the raw `signal` mode
(force=True, bypasses guards), this goes through auto_trader.auto_trade_on_signal
so the scorer, cadence, universe, portfolio cap, and entry-timing gates ALL
apply — identical to the autonomous path. The button is a "nudge", not a
"force".

Usage: python button_trade.py BTC/USDT
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading.signals import auto_trader  # noqa: E402
from trading.signals import multi_signal  # noqa: E402
from trading.tg import bot_ui  # noqa: E402


def run(symbol: str) -> dict:
    # Build a synthetic dip_zone signal from live features so the scorer sees
    # the real market state (not a forced entry).
    try:
        features = multi_signal.market_features(symbol)
        dd = features["drawdown_pct"]
        stype = "dip_zone" if -0.12 < dd <= -0.05 else "deep_dip"
        sig = {
            "signal_type": stype,
            "symbol": symbol,
            "ts_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            "name": stype.upper(),
            "severity": "BIG",
            "emoji": "🟢" if stype == "dip_zone" else "🔴",
            "reason": f"manual button trigger (dd {dd*100:.1f}%)",
            "drawdown_pct": round(dd, 4),
            "regime": features["regime"],
            "atr_ratio": features.get("atr_ratio", 1.0),
        }
        result = auto_trader.auto_trade_on_signal(sig, quote_amount=50.0)
    except Exception as e:  # noqa: BLE001
        result = {"action": "error", "symbol": symbol, "reason": str(e)[:120]}

    # Report the outcome to #alerts.
    if result["action"] == "trade":
        bot_ui.send_card(
            "alerts", f"✅ BUTTON-TRADE {symbol}",
            "<pre>"
            f"order      : {result.get('order_id')}\n"
            f"usdt       : {result.get('amount_usdt'):.2f}\n"
            f"qty        : {result.get('qty')}\n"
            f"price      : {result.get('price')}\n"
            f"multiplier : x{result.get('multiplier')}"
            "</pre>",
            [],
        )
    else:
        bot_ui.send_card(
            "alerts", f"⏭ BUTTON-TRADE {symbol} — skipped",
            f"<pre>{result.get('reason', '')[:200]}</pre>",
            [],
        )
    return result


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "BTC/USDT"
    print(json.dumps(run(sym), indent=2))