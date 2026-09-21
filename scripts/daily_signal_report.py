#!/usr/bin/env python3
"""Daily signal report — the accuracy wrap-up card.

Flow:
  1. Resolve any signals whose horizon elapsed (score past predictions)
  2. Generate today's signals for the universe
  3. Broadcast new signals to #signals (rich cards)
  4. Broadcast the daily wrap-up to #reports (accuracy, actions, pnl)
  5. Persist everything to the signal ledger

Cron: daily 08:10 (after the trading loop + paper report)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading.signals import accuracy as acc  # noqa: E402
from trading.signals import signal_engine as eng  # noqa: E402
from trading.tg import bot_ui  # noqa: E402


def signal_card(sig: dict) -> tuple[str, str, list]:
    """Build a signal card for the #signals topic."""
    emoji = {"BUY": "🟢", "WATCH": "👀", "ERROR": "🔴"}.get(sig["action"], "ℹ️")
    body = (
        "<pre>"
        f"action     : {sig['action']}\n"
        f"bias       : {sig['bias']}\n"
        f"symbol     : {sig['symbol']}\n"
        f"price      : {sig['price']}\n"
        f"drawdown%  : {sig['drawdown_pct']}\n"
        f"regime     : {sig['regime']}\n"
        f"horizon    : {sig['horizon_h']}h\n"
        f"predicted  : {sig['predicted']}\n"
        f"ts_utc     : {sig['ts_utc'][:19]}Z\n"
        f"reason     : {sig['reason']}"
        "</pre>"
    )
    symbol = sig["symbol"]
    tv_url = "https://www.tradingview.com/chart/?symbol=BINANCE:" + symbol.replace("/", "")
    buttons = [
        [{"text": "📈 Chart", "url": tv_url}],
        [{"text": "📊 Accuracy", "callback_data": "cpnl:acc"}],
    ]
    return f"{emoji} SIGNAL {symbol} — {sig['action']}", body, buttons


def daily_card() -> tuple[str, str, list]:
    """Build the daily wrap-up card for #reports."""
    summary = acc.daily_summary()
    stats = acc.accuracy_stats()
    detail_lines = []
    for s in summary["signals"]:
        o = s["outcome"]
        res = f"✅ {o['actual_pct']:+.2f}%" if o and o["correct"] else (
            f"❌ {o['actual_pct']:+.2f}%" if o else "⏳ pending")
        detail_lines.append(f"{s['symbol']:<10} {s['action']:<6} {s['bias']:<8} {s['price']:<10} {res}")
    body = (
        "<pre>"
        f"date        : {summary['date']}\n"
        f"signals     : {summary['signals_today']}\n"
        f"resolved    : {summary['resolved_today']}\n"
        f"acc_today   : {summary['accuracy_today']}% (if resolved)\n"
        f"rolling_acc : {stats.get('accuracy_pct')}% (n={stats.get('recent_n')})\n"
        f"total_logged: {stats.get('resolved')} resolved\n"
        "---- today's signals ----\n"
        + ("\n".join(detail_lines) if detail_lines else "(none yet)")
        + "</pre>"
    )
    buttons = [
        [{"text": "📊 Accuracy", "callback_data": "cpnl:acc"}],
        [{"text": "💵 PnL", "callback_data": "cpnl:pnl"}],
    ]
    return "📅 DAILY SIGNAL REPORT", body, buttons


def accuracy_card() -> tuple[str, str, list]:
    """Build the accuracy breakdown card for #reports."""
    stats = acc.accuracy_stats()
    if not stats.get("resolved"):
        return "📊 ACCURACY", "<i>No resolved signals yet — first results arrive after 24h.</i>", []
    lines = [f"rolling accuracy: {stats['accuracy_pct']}% (n={stats['recent_n']})"]
    lines.append("--- per symbol ---")
    for b in stats["per_symbol"]:
        lines.append(f"{b['bucket']:<10} {b['accuracy']}% (n={b['n']})")
    lines.append("--- per regime ---")
    for b in stats["per_regime"]:
        lines.append(f"{b['bucket']:<10} {b['accuracy']}% (n={b['n']})")
    lines.append("--- per bias ---")
    for b in stats["per_bias"]:
        lines.append(f"{b['bucket']:<10} {b['accuracy']}% (n={b['n']})")
    return "📊 SIGNAL ACCURACY", "<pre>" + "\n".join(lines) + "</pre>", [
        [{"text": "🔄 Refresh", "callback_data": "cpnl:acc"}]
    ]


def run() -> dict:
    # 1. Score past signals whose horizon elapsed.
    eng.resolve_outcomes()

    # 2. Generate today's signals (dedup: one per symbol per day).
    ledger = eng._load_ledger()
    today = datetime.now(timezone.utc).date().isoformat()
    already = {s["symbol"] for s in ledger["signals"] if s["ts_utc"][:10] == today}
    new_sigs = []
    for sym in eng.UNIVERSE:
        if sym in already:
            continue
        try:
            new_sigs.append(eng.generate_signal(sym))
        except Exception as e:  # noqa: BLE001
            print(f"signal error {sym}: {e}")
    if new_sigs:
        for s in new_sigs:
            ledger["signals"].append(s)
        eng._save_ledger(ledger)

    # 3. Broadcast new signals to #signals
    signal_results = []
    for s in new_sigs:
        title, body, buttons = signal_card(s)
        r = bot_ui.send_card("signals", title, body, buttons)
        signal_results.append({"id": s["id"], "ok": r.get("ok")})

    # 4. Broadcast daily wrap-up to #reports
    title, body, buttons = daily_card()
    report_res = bot_ui.send_card("reports", title, body, buttons)

    return {
        "new_signals": len(new_sigs),
        "signal_sent": signal_results,
        "daily_report_ok": report_res.get("ok"),
    }


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))