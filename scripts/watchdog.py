#!/usr/bin/env python3
"""Always-on signal watcher + auto-trader daemon.

Lightweight continuous loop — no cron, no schedule. Every 30 seconds:
  1. Fetch live prices for BTC/ETH/SOL (1 API call each)
  2. Run the 7 detectors → fire signals on state transitions
  3. Auto-trade BIG tradeable signals (dip_zone/deep_dip)
  4. Report new signals + trades to Telegram
  5. Run resilience checks (circuit breaker, balance drift, API health)

If the API goes down → circuit breaker catches it → pause trades → #alerts
If the API recovers → circuit auto-closes after 5 min → resume trading

PM2 keeps this alive. Dies? PM2 restarts. Zero dependency on clock timing.
Designed for ~180 API calls/hour (3 symbols × 60s) — well within rate limits.
"""
from __future__ import annotations

import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading.signals import auto_trader, multi_signal, resilience  # noqa: E402
from trading.tg import bot_ui  # noqa: E402

POLL_SECONDS = 30  # check every 30s — fast enough to feel realtime, conservative on API
ALERT_THROTTLE = 300  # don't spam the same alert more than once per 5 min

_last_alert: dict[str, float] = {}  # key -> last sent timestamp
_shutdown = False


def _throttled_alert(key: str, msg: str) -> None:
    now = time.time()
    if now - _last_alert.get(key, 0) < ALERT_THROTTLE:
        return
    _last_alert[key] = now
    bot_ui.send_card("alerts", "⚠️ WATCHDOG", f"<pre>{msg}</pre>", [])


def _signal_card(sig: dict, trade_result: dict | None = None) -> None:
    meta = multi_signal.SIGNAL_TYPES.get(sig["signal_type"], {})
    emoji = sig.get("emoji", "ℹ️")
    severity = sig.get("severity", "SMALL")
    trade_line = ""
    if trade_result:
        a = trade_result["action"]
        if a == "trade":
            trade_line = (
                f"\ntrade      : ✅ AUTO EXECUTED\n  order={trade_result.get('order_id')}"
                f"\n  usdt={trade_result.get('amount_usdt'):.2f}  qty={trade_result.get('qty')}"
                f"\n  mult=x{trade_result.get('multiplier')}"
            )
        elif a == "skip":
            trade_line = f"\ntrade      : ⏭ {trade_result.get('reason','')[:45]}"

    # Quality score + trading balance for the "clear function" view.
    try:
        from trading.signals.scorer import score_signal
        q = score_signal(sig)
        score_line = f"\nquality    : {q['score']:.0f}/100 ({q['recommended_action']})  EV {q['expected_value_pct']:+.2f}%"
    except Exception:
        score_line = ""
    # Full trade plan: entry/TP/SL/P(win)/size/leverage.
    plan_line = ""
    try:
        from trading.signals.planner import plan_trade
        bal = sig.get("balance_usdt") or 100.0
        plan = plan_trade({**sig, "price": sig.get("price") or 0.0}, bankroll_usdt=bal)
        plan_line = (
            f"\nplan       : {plan.action} @ {plan.entry}\n"
            f"  TP       : {plan.tp} (+{plan.tp_pct}%)\n"
            f"  SL       : {plan.sl} (-{plan.sl_pct}%)\n"
            f"  P(win)   : {plan.p_up*100:.0f}%  EV {plan.ev_pct:+.2f}%  size ${plan.size_usdt}  max {plan.max_leverage}x"
        )
    except Exception:
        pass
    try:
        from trading.exchange.live_client import live_balances
        bal = live_balances()
        bal_line = f"\nbalance    : {bal.get('USDT',0):.2f} USDT available"
    except Exception:
        bal_line = ""

    body = (
        "<pre>"
        f"type       : {sig.get('name', sig['signal_type'])}\n"
        f"severity   : {severity}\n"
        f"symbol     : {sig['symbol']}\n"
        f"price      : {sig.get('price', '?')}\n"
        f"drawdown%  : {sig.get('drawdown_pct', '?')}\n"
        f"ts_utc     : {sig['ts_utc'][:19]}Z\n"
        f"reason     : {sig.get('reason', '')}"
        f"{score_line}"
        f"{plan_line}"
        f"{bal_line}"
        f"{trade_line}"
        "</pre>"
    )
    sym = sig["symbol"]
    tv = "https://www.tradingview.com/chart/?symbol=BINANCE:" + sym.replace("/", "")
    prefix = "🔴" if severity == "BIG" else "🔵"
    buttons = [
        [{"text": "📈 Chart", "url": tv},
         {"text": "✅ Auto-Trade", "callback_data": f"ctrade:{sym}"}],
    ]
    bot_ui.send_card("signals", f"{prefix} {emoji} {sym} — {sig.get('name', sig['signal_type'])}", body, buttons)


def _trade_confirmation(sig: dict, trade_result: dict) -> None:
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
        [[{"text": "📈 Chart", "url": "https://www.tradingview.com/chart/?symbol=BINANCE:" + sig["symbol"].replace("/", "")}]],
    )


def tick() -> dict:
    """One cycle of the continuous watcher."""
    now_sec = int(time.time())
    # 1. API health quick-check — if balance fetch fails, circuit breaker logs it.
    # Use the LIVE client: it is the leg that actually places real orders, so
    # its health is what matters. (testnet.binance.vision was cycling failures
    # and opening the circuit breaker on a phantom outage.)
    try:
        from trading.exchange.live_client import live_balances
        live_balances()  # smoke test
    except Exception as e:
        resilience.record_failure(f"balance check: {str(e)[:100]}")
        _throttled_alert("api_down", f"API unreachable — {str(e)[:120]}")
        return {"tick": "api_unreachable"}

    # 2. Real-time micro-sampling — feed the timing optimizer (every tick).
    try:
        from trading.signals import realtime
        for sym in multi_signal.UNIVERSE:
            p = realtime.sample(sym)
            if p is not None:
                realtime.update_sample(sym, p)
    except Exception:
        pass  # sampling is best-effort; don't block trading on it

    # 3. Scan signals (dedup state machine handles transitions).
    new_signals = multi_signal.scan_all()

    # 4. Auto-trade tradeable signals.
    trades = 0
    for sig in new_signals:
        trade_result = None
        if sig["signal_type"] in auto_trader.TRADEABLE_SIGNALS:
            try:
                if resilience.circuit_open():
                    _throttled_alert("circuit", "Circuit BREAKER open — trades paused")
                    trade_result = {"action": "skip", "reason": "circuit_breaker_open"}
                else:
                    trade_result = auto_trader.auto_trade_on_signal(sig)
                    if trade_result["action"] == "trade":
                        trades += 1
                        _trade_confirmation(sig, trade_result)
            except Exception as e:
                resilience.record_failure(str(e)[:200])
                _throttled_alert("trade_fail", f"Trade failed: {str(e)[:120]}")
                trade_result = {"action": "error", "reason": str(e)[:100]}
        _signal_card(sig, trade_result)

    # 4b. Short-term track — run the quick-trade cycle (throttled to ~2 min).
    try:
        if now_sec % 120 < POLL_SECONDS:
            import subprocess
            subprocess.Popen(
                [sys.executable, "scripts/short_term_track.py", "trade"],
                cwd=str(Path(__file__).resolve().parents[1]),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass  # short-term track is best-effort; don't block the main loop

    # 5. Balance-drift check (once per tick, throttled to ~5 min).
    now = int(time.time())
    if now % 300 < POLL_SECONDS:  # rough every 5 min
        drift = resilience.check_balance_drift()
        if not drift.get("ok"):
            _throttled_alert("drift", f"Balance drift: {drift.get('drift')} BTC")

    return {"tick": "ok", "new_signals": len(new_signals), "auto_trades": trades,
            "ts_utc": datetime.now(timezone.utc).isoformat()}


def main_loop() -> None:
    print(f"[WATCHDOG] starting — poll every {POLL_SECONDS}s (ctrl+c to stop)")
    while not _shutdown:
        try:
            result = tick()
            if result.get("new_signals", 0) > 0 or result.get("auto_trades", 0) > 0:
                print(f"[WATCHDOG] {result['ts_utc'][:19]}Z "
                      f"signals={result.get('new_signals')} trades={result.get('auto_trades')} "
                      f"circuit={resilience.circuit_open()}")
        except Exception as e:
            print(f"[WATCHDOG] tick error: {str(e)[:120]}")
            resilience.record_failure(str(e)[:200])
        time.sleep(POLL_SECONDS)


def _on_signal(sig_no, frame):
    global _shutdown
    _shutdown = True
    print("\n[WATCHDOG] shutdown signal received — exiting cleanly")


signal.signal(signal.SIGINT, _on_signal)
signal.signal(signal.SIGTERM, _on_signal)

if __name__ == "__main__":
    main_loop()