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


def _check_live_capital() -> tuple[bool, str]:
    """Is REAL money actually trading, or is it silently stuck?

    Added 2026-10-03 by the autonomous improvement agent. Found by reading
    live-trades.json + unified.log: the live spot executor had not opened or
    closed a real trade in 24 days while the paper book traded every few
    minutes, because ~86% of the ~$20.68 real equity was stuck in NEAR/ARB
    (never sold, and NOT in live_executor.py ALLOWED_REAL so they can never be
    rotated) leaving only $0.03 free USDT — below the $6 order minimum. Every
    other heartbeat component said OK, so the owner never heard that real
    trading had completely stopped. This component is that voice.

    LOCAL-ONLY (reads state files; no exchange call) so it is fast and cannot
    rate-limit or hang the daily card.
    """
    if not (ROOT / "config" / "LIVE_ENABLED").exists():
        return True, "live trading intentionally off"
    try:
        lt = json.loads((ROOT / "data" / "live-trades.json").read_text())
    except Exception:
        return True, "no live-trades state"
    newest = None
    for t in (lt.get("closed") or []) + (lt.get("open") or []):
        s = t.get("close_ts_utc") or t.get("open_ts_utc")
        if not s:
            continue
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if newest is None or d > newest:
                newest = d
        except Exception:
            continue
    if newest is None:
        return True, "no live trades yet"
    age_h = (datetime.now(timezone.utc) - newest).total_seconds() / 3600
    # is the paper path still trading? (dead-live + live-paper = the signature)
    paper_h = None
    try:
        up = json.loads((ROOT / "data" / "unified-paper.json").read_text())
        pnew = None
        for t in (up.get("closed") or []):
            s = t.get("ts_utc")
            if not s:
                continue
            try:
                d = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if pnew is None or d > pnew:
                    pnew = d
            except Exception:
                continue
        if pnew is not None:
            paper_h = (datetime.now(timezone.utc) - pnew).total_seconds() / 3600
    except Exception:
        pass
    if age_h > 7 * 24 and paper_h is not None and paper_h < 72:
        free = "?"
        try:
            lb = json.loads((ROOT / "data" / "live-balance-cache.json").read_text())
            free = lb.get("usdt")
        except Exception:
            pass
        return False, (f"DEAD {age_h/24:.0f}d (paper traded {paper_h:.0f}h ago) "
                       f"— untradeable bag / free USDT {free} < $6 order min")
    return True, f"last real trade {age_h:.0f}h ago"


def main():
    always = "--always" in sys.argv
    checks = [
        ("data", *_check_data_health()),
        ("processes", *_check_processes()),
        ("circuit", *_check_circuit()),
        ("adaptive", *_check_adaptive()),
        ("market", *_check_market_watch()),
        ("capital", *_check_live_capital()),
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
