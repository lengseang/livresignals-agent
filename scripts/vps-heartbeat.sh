#!/usr/bin/env bash
# VPS heartbeat: sends a daily alive-ping to Telegram.
# Called by PM2 cron 09:00 daily. If this stops arriving for 24h -> dead box.
set -uo pipefail
cd /opt/trading-agent || exit 1
STATUS=$(PYTHONPATH=src venv/bin/python -m trading.data.health 2>/dev/null | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print('OK' if d.get('ok') else 'ISSUES: '+'; '.join(d.get('problems',[])[:3]))" 2>/dev/null || echo "health n/a")

# Use hermes send if the gateway config exists on the box; else curl fallback.
# FIXED: cron/PM2 runs with a bare PATH, so `command -v hermes` missed
# /usr/local/bin/hermes and the daily ping was silently skipped.
HERMES_BIN="$(command -v hermes 2>/dev/null || true)"
[ -z "$HERMES_BIN" ] && [ -x /usr/local/bin/hermes ] && HERMES_BIN=/usr/local/bin/hermes
[ -z "$HERMES_BIN" ] && [ -x /home/ubuntu/hermes-venv/bin/hermes ] && HERMES_BIN=/home/ubuntu/hermes-venv/bin/hermes

# Route through tg-send.sh, which resolves the heartbeat TOPIC from
# config/topic-routing.json (group -1004498352646 : thread 9).
#
# FIXED: this used to send to bare `--to telegram`, which resolves to the
# DM/home channel and ignored the topic map entirely, so the heartbeat
# never appeared in the heartbeat topic.
#
# Also fixed: the old version redirected to /dev/null and only signalled
# failure on a non-zero exit, so an accepted-but-undelivered send looked
# identical to success. Output is now captured and echoed for audit.
TG_SEND="/opt/trading-agent/scripts/tg-send.sh"

if [ -x "$TG_SEND" ]; then
  SEND_OUT=$(HERMES_HOME="${HERMES_HOME:-/home/ubuntu/hermes-home}" \
    "$TG_SEND" heartbeat "🫀 VPS Heartbeat" \
    "Alive on $(hostname) | IP $(hostname -I | awk '{print $1}') | Data health: $STATUS" 2>&1)
  echo "heartbeat: tg-send rc=$? :: $SEND_OUT"
else
  echo "heartbeat: tg-send.sh missing at $TG_SEND, ping skipped (data health: $STATUS)"
fi
echo "heartbeat done: $STATUS"