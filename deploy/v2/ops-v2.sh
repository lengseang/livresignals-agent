#!/usr/bin/env bash
# V2 ops jobs (pm2 cron): `watchdog` every 5 minutes, `daily` once a day (scorecard + digest). Read-only + alerts.
# Runs for every paper lane that exists: A (approved fixed bracket) and B (volatility-scaled, owner decision E16).
set -uo pipefail
ROOT="${V2_ROOT:-/opt/trading-agent-v2}"
cd "$ROOT" || exit 0
[ -f "$ROOT/config/v2.env" ] && { set -a; . "$ROOT/config/v2.env"; set +a; }
: "${V2_OUT:=$ROOT/data/v2/paper}" "${V2_OUT_B:=$ROOT/data/v2/paper-b}"
export PYTHONPATH="$ROOT/src"
PY="${V2_PY:-$ROOT/venv/bin/python}"   # venv is a symlink to the box venv (see RUNBOOK)
for OUT in "$V2_OUT" "$V2_OUT_B"; do
  [ -d "$OUT" ] || continue
  case "${1:-watchdog}" in
    watchdog) "$PY" -m trading.v2.watchdog --out "$OUT" ;;
    daily)    "$PY" -m trading.v2.scorecard --out "$OUT" || true
              "$PY" -m trading.v2.watchdog --out "$OUT" --daily ;;
  esac
done
exit 0
