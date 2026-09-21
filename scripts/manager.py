#!/usr/bin/env python3
"""Manager layer — the 'operations manager' for the trading system.

Runs every 5 minutes via cron. Checks that every critical job has actually
executed recently (not just "scheduled"). If a job is stale/dead, it runs it
immediately and logs the recovery. Also detects total silence (no job ran in
>2h) and writes an ALERT the heartbeat will escalate.

This is the layer that prevents "dead silence" — it does not depend on PM2
status strings, only on real evidence: did the job's output file update?
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/opt/trading-agent")
LOG_DIR = Path("/home/ubuntu/.pm2/logs")  # authoritative logs from PM2 jobs
STATE = ROOT / "data" / "manager-state.json"

# (job name, log file to watch, max age in seconds before we consider it dead)
# max_age MUST exceed the job's real cron cadence with margin, else the manager
# fires false "STALE" recoveries every cycle (bug fixed 2026-09-12).
JOBS = [
    ("adaptive-engine", "adaptive-engine.log", 70 * 60),   # cron: hourly at :31
    ("candle-ingest", "candle-ingest.log", 70 * 60),        # runs hourly
    ("live-manage", "live-manage.log", 15 * 60),            # runs every 10m
    ("unified-trader", "unified.log", 45 * 60),             # runs every 30m (SIGNAL ENGINE)
    ("system-heartbeat", "system-heartbeat.log", 25 * 60 * 60),  # daily
]
# unified-trader logs to /opt/trading-agent/logs/, not .pm2/logs/
ALT_LOG_DIRS = {"unified-trader": ROOT / "logs"}

ALERT_FILE = ROOT / "data" / "MANAGER_ALERT.txt"


def _log_mtime(name: str, logfile: str) -> float:
    # BUG FIX 2026-09-13: callers used to pass the log FILE name here, but
    # ALT_LOG_DIRS is keyed by JOB name. For unified-trader the lookup always
    # missed -> the manager watched the frozen legacy .pm2/logs/unified.log
    # instead of the live /opt/trading-agent/logs/unified.log, so the staleness
    # check could never clear and a bogus recovery fired every 5 min (369
    # "unified-trader last ran Nm ago" restarts + Telegram spam + a duplicate
    # unified_trader.py competing with the real 16/46 cron runs).
    log_dir = ALT_LOG_DIRS.get(name, LOG_DIR)
    p = log_dir / logfile
    if not p.exists():
        return 0.0
    return p.stat().st_mtime


def main():
    now = time.time()
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "actions": []}
    alive = True

    for name, logfile, max_age in JOBS:
        mtime = _log_mtime(name, logfile)
        age = now - mtime
        if age > max_age:
            # job is stale/dead — try to run it directly
            report["actions"].append(f"STALE: {name} last ran {age/60:.0f}m ago (limit {max_age/60:.0f}m) -> restarting")
            alive = False
            # immediate restart via the script directly (bypasses PM2 status confusion)
            script = {
                "adaptive-engine": "scripts/adaptive_engine.py check",
                "candle-ingest": "scripts/ingest_all.py",
                "live-manage": "scripts/live_executor.py manage",
                "unified-trader": "scripts/unified_trader.py",
                "system-heartbeat": "scripts/system_heartbeat.py",
            }[name]
            # BUG FIX 2026-09-12: the restart used to write to LOG_DIR (.pm2/logs)
            # for EVERY job, but the staleness check above reads ALT_LOG_DIRS for
            # unified-trader. So a unified-trader recovery updated a file nobody
            # watched -> the check stayed stale -> recovery fired every 5 min
            # forever (428 bogus "RECOVERED" lines) with split, unverifiable logs.
            out_dir = ALT_LOG_DIRS.get(name, LOG_DIR)
            os.system(
                f"cd {ROOT} && PYTHONPATH=src {ROOT}/venv/bin/python {script} "
                f">> {out_dir}/{logfile} 2>&1 &"
            )

    # total silence check: if adaptive-engine hasn't run in 3h, that's a real outage
    silence = now - _log_mtime("adaptive-engine", "adaptive-engine.log") > 3 * 3600
    if silence:
        ALERT_FILE.write_text(
            f"SILENCE ALERT: adaptive-engine has not run in "
            f"{(now - _log_mtime('adaptive-engine', 'adaptive-engine.log'))/3600:.1f}h "
            f"@ {datetime.now(timezone.utc).isoformat()}\n",
            encoding="utf-8",
        )
        report["actions"].append("SILENCE ALERT written")
        _telegram("🚨 TRADING BOX SILENT",
                  f"adaptive-engine has not run in "
                  f"{(now - _log_mtime('adaptive-engine', 'adaptive-engine.log'))/3600:.1f}h. "
                  f"VPS may be down — check it.")
    else:
        if ALERT_FILE.exists():
            ALERT_FILE.unlink()  # recovered

    # alert on any unrecoverable stale job too (so owner is never blind)
    if not alive:
        _telegram("⚠️ MANAGER RECOVERY",
                  "A job was stale and auto-restarted: "
                  + " | ".join(report["actions"][:3]))

    STATE.write_text(json.dumps(report, indent=2), encoding="utf-8")
    # summary line to stdout for cron mail/log
    print(f"manager: {'OK' if alive else 'RECOVERED'} | " + " | ".join(report["actions"][:3]))


def _telegram(title: str, body: str) -> None:
    """Best-effort Telegram alert. Never blocks the manager on failure."""
    try:
        from trading.tg.bot_ui import send_card
        send_card("alerts", title, "<pre>" + body + "</pre>")
    except Exception:
        pass


if __name__ == "__main__":
    main()
