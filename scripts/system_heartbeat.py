#!/usr/bin/env python3
"""Daily system heartbeat — one honest line proving the trader is alive.

Checks every moving part and reports pass/fail. Sends a Telegram card if any
component is broken (silent when everything is healthy, to avoid daily noise —
or always send if --always is passed).

Usage:
  system_heartbeat.py            # silent unless something is broken
  system_heartbeat.py --always   # always send the status card
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ROOT = Path(__file__).resolve().parents[1]


def _check_data_health() -> tuple[bool, str]:
    try:
        out = subprocess.run(
            [str(ROOT / "venv/bin/python"), "-m", "trading.data.health"],
            cwd=str(ROOT), env={"PYTHONPATH": str(ROOT / "src")},
            capture_output=True, text=True, timeout=60,
        )
        d = json.loads(out.stdout)
        if d.get("ok"):
            return True, "data fresh"
        return False, f"{len(d.get('problems', []))} stale series"
    except Exception as e:  # noqa: BLE001
        return False, f"health check failed: {str(e)[:60]}"


def _check_processes() -> tuple[bool, str]:
    try:
        out = subprocess.run(["pm2", "jlist"], capture_output=True, text=True, timeout=20)
        procs = json.loads(out.stdout)
        # The adaptive-engine + candle-ingest CRON jobs replace the old
        # watchdog (which was a redundant legacy scanner). For cron jobs,
        # "online", "waiting restart" (between runs), and "stopped" (between
        # cron ticks) are all healthy — only a truly unknown/failed state is bad.
        wanted = {"adaptive-engine", "candle-ingest", "live-manage"}
        states = {p["name"]: p["pm2_env"]["status"] for p in procs}
        bad = []
        for name in wanted:
            st = states.get(name)
            if st not in ("online", "waiting restart", "stopped"):
                bad.append(f"{name}={st}")
        if bad:
            return False, "; ".join(bad)
        return True, "processes up"
    except Exception as e:  # noqa: BLE001
        return False, f"pm2 check failed: {str(e)[:60]}"


def _check_circuit() -> tuple[bool, str]:
    p = ROOT / "data" / "resilience-state.json"
    if not p.exists():
        return True, "no resilience state"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if d.get("consecutive_failures", 0) >= 5:
            return False, f"circuit failing ({d['consecutive_failures']} consecutive)"
        return True, f"circuit ok ({d.get('consecutive_failures', 0)} consecutive fails)"
    except (json.JSONDecodeError, OSError):
        return True, "resilience state unreadable"


def _check_adaptive() -> tuple[bool, str]:
    p = ROOT / "data" / "adaptive-state.json"
    if not p.exists():
        return False, "no adaptive state"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        regime = d.get("regime", "?")
        trades = len(d.get("closed_trades", []))
        budget = d.get("budget", 0)
        return True, f"regime={regime} trades={trades} budget=${budget:.2f}"
    except (json.JSONDecodeError, OSError):
        return False, "adaptive state unreadable"


def _check_market_watch() -> tuple[bool, str]:
    """Report whether a validated trade signal is active or the bot is calmly
    waiting. This keeps the owner informed even on idle days (no dead silence)."""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from unified_trader import _features, _validated
        from trading.exchange.live_client import make_live_exchange
        ex = make_live_exchange()
        ex.load_markets()
        best = None
        for sym in ["BTC/USDT", "ETH/USDT", "XRP/USDT", "SOL/USDT"]:
            for tf in ["5m", "15m", "30m"]:
                sig = _features(ex, sym, tf)
                if not sig:
                    continue
                edge = _validated(sig)
                if edge:
                    best = f"{sym} {tf} dd={sig['dd']*100:.1f}% rsi={sig['rsi']:.0f}"
                    break
            if best:
                break
        if best:
            return True, f"SIGNAL READY: {best}"
        return True, "calm — waiting for validated dip (no forced trades)"
    except Exception as e:  # noqa: BLE001
        return True, f"watch check skipped: {str(e)[:50]}"


def main():
    always = "--always" in sys.argv
    checks = [
        ("data", *_check_data_health()),
        ("processes", *_check_processes()),
        ("circuit", *_check_circuit()),
        ("adaptive", *_check_adaptive()),
        ("market", *_check_market_watch()),
    ]
    all_ok = all(ok for _, ok, _ in checks)

    lines = [f"time     : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"]
    for name, ok, msg in checks:
        mark = "OK " if ok else "FAIL"
        lines.append(f"[{mark}] {name:<10}: {msg}")

    body = "\n".join(lines)
    print(body)

    if not all_ok or always:
        try:
            from trading.tg.bot_ui import send_card
            title = "🫀 SYSTEM OK" if all_ok else "⚠️ SYSTEM ISSUES"
            send_card("reports", title, "<pre>" + body + "</pre>")
        except Exception as e:  # noqa: BLE001
            print(f"(telegram send failed: {e})")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
