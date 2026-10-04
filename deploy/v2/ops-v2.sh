#!/usr/bin/env bash
# V2 ops jobs (pm2 cron): `watchdog` every 5 minutes, `daily` once a day (scorecard + digest). Read-only + alerts.
set -uo pipefail
ROOT="${V2_ROOT:-/opt/trading-agent-v2}"
cd "$ROOT" || exit 0
[ -f "$ROOT/config/v2.env" ] && { set -a; . "$ROOT/config/v2.env"; set +a; }
: "${V2_OUT:=$ROOT/data/v2/paper}"
export PYTHONPATH="$ROOT/src"
PY="${V2_PY:-$ROOT/venv/bin/python}"   # venv is a symlink to the box venv (see RUNBOOK)
case "${1:-watchdog}" in
  watchdog) "$PY" -m trading.v2.watchdog --out "$V2_OUT" ;;
  daily)    "$PY" -m trading.v2.scorecard --out "$V2_OUT" || true
            "$PY" -m trading.v2.watchdog --out "$V2_OUT" --daily ;;
esac
exit 0
