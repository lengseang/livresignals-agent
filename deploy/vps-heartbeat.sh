#!/usr/bin/env bash
# VPS heartbeat: sends a daily alive-ping to Telegram.
# Called by PM2 cron 09:00 daily. If this stops arriving for 24h -> dead box.
set -uo pipefail
cd /opt/trading-agent || exit 1
STATUS=$(PYTHONPATH=src venv/bin/python -m trading.data.health 2>/dev/null | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print('OK' if d.get('ok') else 'ISSUES: '+'; '.join(d.get('problems',[])[:3]))" 2>/dev/null || echo "health n/a")

# Use hermes send if the gateway config exists on the box; else curl fallback.
if command -v hermes >/dev/null 2>&1; then
  hermes send --to telegram --subject "🫀 VPS Heartbeat" \
    "Alive on $(hostname) | IP $(hostname -I | awk '{print $1}') | Data health: $STATUS" >/dev/null 2>&1 || true
else
  echo "heartbeat: hermes not installed yet, ping skipped (data health: $STATUS)"
fi
echo "heartbeat done: $STATUS"