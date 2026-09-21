#!/usr/bin/env bash
# Topic-aware Telegram sender for the VPS bot (Bot B / HERTRADE).
# Usage: tg-send.sh <topic> <subject> <message>
#   topics: signals | pnl | alerts | heartbeat | reports | chat
# Reads routing from config/topic-routing.json (group + thread ids).
set -uo pipefail
cd /opt/trading-agent 2>/dev/null || cd "$(dirname "$0")/.."
export HERMES_HOME="${HERMES_HOME:-/home/ubuntu/hermes-home}"
export PATH="$HOME/hermes-venv/bin:$PATH"

TOPIC="${1:?topic required}"
SUBJECT="${2:-}"
MSG="${3:-}"

GROUP=$(python3 -c "import json;print(json.load(open('config/topic-routing.json'))['group']['id'])")
THREAD=$(python3 -c "import json;print(json.load(open('config/topic-routing.json'))['topics']['$TOPIC'])")
TARGET="telegram:$GROUP:$THREAD"

if [ -n "$SUBJECT" ] && [ -n "$MSG" ]; then
  hermes send --to "$TARGET" --subject "$SUBJECT" "$MSG" 2>&1 | tail -1
else
  echo "tg-send: need subject + message"
  exit 1
fi