#!/usr/bin/env bash
# VPS nightly backup: copy SQLite + ledgers + reports into a timestamped tar,
# keep last 14, and (optionally) push to the local PC via scp.
# Called by PM2 cron 03:00 daily.
set -uo pipefail
cd /opt/trading-agent || exit 1
TS=$(date +%Y%m%d-%H%M%S)
BK=/opt/trading-agent/backups
mkdir -p "$BK"

tar -czf "$BK/trading-$TS.tar.gz" \
  data/trading.db data/pa-testnet-ledger.json data/pa-pnl-history.csv \
  data/pa-replay-pnl.csv data/pa-paper-state.json \
  reports 2>/dev/null || true

# Keep only 14 most recent
ls -1t "$BK"/trading-*.tar.gz 2>/dev/null | tail -n +15 | xargs -r rm -f

echo "backup done: $BK/trading-$TS.tar.gz ($(du -h "$BK/trading-$TS.tar.gz" | cut -f1))"