#!/usr/bin/env bash
# Heartbeat via topic bus (replaces old home-channel version)
set -uo pipefail
cd /opt/trading-agent
STATUS=$(PYTHONPATH=src venv/bin/python -m trading.data.health 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(\"OK\" if d.get(\"ok\") else \"ISSUES: \"+\"; \".join(d.get(\"problems\",[])[:3]))" 2>/dev/null || echo "n/a")
HOST_IP=$(hostname -I | awk "{print \$1}")
bash scripts/tg-send.sh heartbeat "🫀 VPS Heartbeat" "Alive on $(hostname) | IP $HOST_IP | Data health: $STATUS"
