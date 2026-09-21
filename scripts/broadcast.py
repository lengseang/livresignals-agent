#!/usr/bin/env python3
"""Broadcast rich bot cards to the Trading Signals topics.

Usage:
  python3 broadcast.py menu          # install /commands menu
  python3 broadcast.py signal <json> # send a signal card from an entry json
  python3 broadcast.py pnl           # send current PnL card
  python3 broadcast.py status        # send system status card
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from trading.tg import bot_ui  # noqa: E402

COMMANDS = [
    ("start", "Hello / menu"),
    ("signals", "Latest trading signal"),
    ("pnl", "Portfolio PnL snapshot"),
    ("status", "System status (VPS, gateway, health)"),
    ("dashboard", "Live dashboard snapshot + open web UI"),
    ("heartbeat", "Check bot is alive"),
]


def cmd_menu() -> dict:
    return bot_ui.set_command_menu(COMMANDS)


def cmd_signal(entry: dict) -> dict:
    title, body, buttons = bot_ui.signal_card(entry)
    return bot_ui.send_card("signals", title, body, buttons)


def cmd_pnl() -> dict:
    from trading.allocation.pnl import compute_pnl
    report = compute_pnl()
    title, body, buttons = bot_ui.pnl_card(report)
    return bot_ui.send_card("pnl", title, body, buttons)


def cmd_status() -> dict:
    import socket
    import subprocess
    status = {"vps": socket.gethostname(), "gateway": "pm2", "trading": "testnet-paper"}
    try:
        r = subprocess.run(
            [sys.executable, "-m", "trading.data.health"],
            capture_output=True, text=True, timeout=30,
        )
        d = json.loads(r.stdout)
        status["data_health"] = "OK" if d.get("ok") else str(d.get("problems", [])[:2])
    except Exception as e:  # noqa: BLE001
        status["data_health"] = f"n/a ({e})"
    from trading.allocation.testnet_executor import load_ledger, LEDGER_FILE
    status["ledger_entries"] = len(load_ledger().get("entries", []))
    try:
        from trading.exchange.testnet_client import testnet_balances
        b = testnet_balances()
        status["wallet"] = f"USDT {b.get('USDT',0):.2f} BTC {b.get('BTC',0):.6f} ETH {b.get('ETH',0):.6f} SOL {b.get('SOL',0):.6f}"
    except Exception as e:  # noqa: BLE001
        status["wallet"] = f"n/a ({e})"
    title, body, buttons = bot_ui.status_card(status)
    return bot_ui.send_card("chat", title, body, buttons)


def cmd_acc() -> dict:
    """Broadcast the signal accuracy card to #reports."""
    import sys as _sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from daily_signal_report import accuracy_card
    title, body, buttons = accuracy_card()
    return bot_ui.send_card("reports", title, body, buttons)


def cmd_params() -> dict:
    """Broadcast the adaptive params card to #reports."""
    from trading.signals.adaptive import params_card
    title, body, buttons = params_card()
    return bot_ui.send_card("reports", title, body, buttons)


def cmd_dashboard() -> dict:
    """Live dashboard snapshot card + button to open the web UI."""
    # Pull the same data the web dashboard shows.
    data = {}
    try:
        from trading.allocation.pnl import compute_pnl
        port = compute_pnl().get("portfolio", {})
        data["paper_pnl"] = port.get("unrealized_pnl", 0)
        data["paper_pct"] = port.get("unrealized_pnl_pct", 0)
        data["paper_cost"] = port.get("cost_basis", 0)
        data["paper_value"] = port.get("market_value", 0)
    except Exception as e:  # noqa: BLE001
        data["paper_pnl"] = None
    try:
        import json as _j
        st = _j.loads((Path(__file__).resolve().parents[1] / "data" / "short-term-state.json").read_text())
        data["st_budget"] = st.get("budget", 500.0)
        data["st_trades"] = len(st.get("closed_trades", []))
    except Exception:  # noqa: BLE001
        data["st_budget"] = 500.0
    try:
        from trading.exchange.live_client import live_balances, make_live_exchange
        b = live_balances(); ex = make_live_exchange()
        btc = b.get("BTC", 0)
        t = ex.publicGetTickerPrice({"symbol": "BTCUSDT"})
        data["live_btc"] = btc
        data["live_total"] = round(btc * float(t["price"]), 2)
    except Exception:  # noqa: BLE001
        data["live_total"] = None

    sign = "+" if (data.get("paper_pnl") or 0) >= 0 else ""
    body = (
        "<pre>"
        f"📈 Save & Grow : {sign}{data.get('paper_pnl', 0):.2f} ({data.get('paper_pct', 0):+.2f}%)\n"
        f"   (${data.get('paper_cost', 0):.2f} -> ${data.get('paper_value', 0):.2f})\n\n"
        f"⚡ Quick Trades: ${data.get('st_budget', 500):.2f} ({data.get('st_trades', 0)} closed)\n\n"
        f"💰 Live BTC    : {data.get('live_total', 'n/a')}"
        "</pre>"
    )
    buttons = [
        [{"text": "🌐 Open Web Dashboard", "url": "https://rehab-buffalo-efficiency-fixtures.trycloudflare.com/"}],
        [{"text": "💵 PnL", "callback_data": "cpnl:pnl"},
         {"text": "📊 Accuracy", "callback_data": "cpnl:acc"}],
    ]
    return bot_ui.send_card("chat", "📊 DASHBOARD SNAPSHOT", body, buttons)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "menu"
    if mode == "menu":
        print(json.dumps(cmd_menu()))
    elif mode == "pnl":
        print(json.dumps(cmd_pnl()))
    elif mode == "status":
        print(json.dumps(cmd_status()))
    elif mode == "acc":
        print(json.dumps(cmd_acc()))
    elif mode == "params":
        print(json.dumps(cmd_params()))
    elif mode == "dashboard":
        print(json.dumps(cmd_dashboard()))
    elif mode == "signal" and len(sys.argv) > 2:
        entry = json.loads(sys.argv[2])
        print(json.dumps(cmd_signal(entry)))
    else:
        print("usage: broadcast.py menu|pnl|status|acc|params|signal <json>")