#!/usr/bin/env bash
# V2 PAPER runner (simulated book, public market data; NO real orders exist in trading.v2).
# Usage: run-v2.sh [a|b]   a = approved fixed 3.5%/7% bracket (default); b = volatility-scaled bracket (owner decision E16)
# Started by pm2 from deploy/v2/ecosystem.v2.config.js. Refuses to start if preflight fails.
set -euo pipefail
ROOT="${V2_ROOT:-/opt/trading-agent-v2}"
cd "$ROOT"
ENV_FILE="$ROOT/config/v2.env"            # git-ignored (*.env); chmod 600; holds TYPESAFE_API_KEY
if [ ! -f "$ENV_FILE" ]; then echo "missing $ENV_FILE (copy deploy/v2/v2.env.example)"; exit 1; fi
set -a; . "$ENV_FILE"; set +a
: "${V2_OUT:=$ROOT/data/v2/paper}" "${V2_OUT_B:=$ROOT/data/v2/paper-b}" "${V2_SYMBOLS:=BTCUSDT,ETHUSDT,SOLUSDT}" "${V2_CADENCE:=15}"
LANE="${1:-a}"
case "$LANE" in
  a) EXIT_MODE=fixed ;;
  b) EXIT_MODE=atr; V2_OUT="$V2_OUT_B" ;;
  *) echo "unknown lane $LANE (a|b)"; exit 2 ;;
esac
export PYTHONPATH="$ROOT/src"
PY="${V2_PY:-$ROOT/venv/bin/python}"   # venv is a symlink to the box venv (see RUNBOOK)
if ! "$PY" -m trading.v2.preflight --out "$V2_OUT" --symbols "$V2_SYMBOLS"; then
  "$PY" -m trading.v2.notify "preflight FAILED" "V2 paper lane $LANE did not start on $(hostname); see $V2_OUT/preflight.json" || true
  sleep "${V2_FAIL_SLEEP:-300}"             # pm2 restarts us; do not hammer
  exit 1
fi
"$PY" -m trading.v2.notify "starting" "V2 paper lane $LANE ($EXIT_MODE exits) starting on $(hostname): $V2_SYMBOLS every ${V2_CADENCE}m" || true
# shellcheck disable=SC2086  (V2_EXTRA_ARGS is for smoke tests only, e.g. "--minutes 3")
exec "$PY" -m trading.v2.runner --approved --symbols "$V2_SYMBOLS" --cadence "$V2_CADENCE" --out "$V2_OUT" --exit-mode "$EXIT_MODE" ${V2_EXTRA_ARGS:-}
