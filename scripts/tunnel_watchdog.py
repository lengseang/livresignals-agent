#!/usr/bin/env python3
"""Tunnel URL watchdog — re-send the dashboard link to Telegram if it changes.

Lightweight: runs every 5 min (PM2 cron). Reads the current cloudflared
trycloudflare URL from the PM2 log; if it differs from the last known URL,
sends a fresh dashboard card to Telegram with the new link. This keeps the
"Open Web Dashboard" button always pointing at a live URL across VPS reboots,
without any heavy process.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE = Path(__file__).resolve().parents[1] / "data" / "tunnel-url.json"


def _current_url() -> str | None:
    # cloudflared logs the URL to the error log (combined output).
    paths = [
        Path("/home/ubuntu/.pm2/logs/cloudflared-error.log"),
        Path("/home/ubuntu/.pm2/logs/cloudflared-out.log"),
    ]
    for p in paths:
        if p.exists():
            try:
                txt = p.read_text(encoding="utf-8", errors="ignore")
                m = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", txt)
                if m:
                    return m[-1]
            except OSError:
                pass
    return None


def main() -> None:
    url = _current_url()
    if not url:
        return  # tunnel down — nothing to do
    last = None
    if STATE.exists():
        try:
            last = json.loads(STATE.read_text(encoding="utf-8")).get("url")
        except (json.JSONDecodeError, OSError):
            pass
    if url == last:
        return  # unchanged — stay quiet
    # URL changed (or first run): persist + send fresh card to Telegram.
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"url": url, "updated_utc": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    subprocess.run(
        [sys.executable, "scripts/broadcast.py", "dashboard"],
        cwd="/opt/trading-agent", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


if __name__ == "__main__":
    main()
