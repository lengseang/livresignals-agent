#!/usr/bin/env bash
# VPS nightly backup.
#
# WHAT CHANGED 2026-09-21
# The original version backed up ONLY data/trading.db, some pa-*.json files and
# reports/ — into backups/, on the SAME disk. So:
#   * the code (scripts/, src/) was never backed up at all
#   * a backup on the same volume dies with the volume
# This version adds a git BUNDLE of the whole repo (one file, full history) so
# the code has a single artefact that can be copied off-box.
#
# STILL LOCAL. Copying off-box is the remaining step — see the migration plan.
# Run it with `--offbox HOST` to also rsync the bundle to another machine.
set -uo pipefail
cd /opt/trading-agent || exit 1

TS=$(date +%Y%m%d-%H%M%S)
BK=/opt/trading-agent/backups
mkdir -p "$BK"

# ---- 1. the irreplaceable data -------------------------------------------
tar -czf "$BK/trading-$TS.tar.gz" \
  data/trading.db data/pa-testnet-ledger.json data/pa-pnl-history.csv \
  data/pa-replay-pnl.csv data/pa-paper-state.json \
  reports 2>/dev/null || true
echo "data backup : $BK/trading-$TS.tar.gz ($(du -h "$BK/trading-$TS.tar.gz" 2>/dev/null | cut -f1))"

# ---- 2. the CODE — a git bundle carries full history in ONE file ----------
# Previously absent entirely. A bundle can be cloned from as if it were a
# remote, so the code is fully recoverable from this single file.
if [ -d .git ]; then
  git bundle create "$BK/code-$TS.bundle" --all 2>/dev/null \
    && echo "code bundle : $BK/code-$TS.bundle ($(du -h "$BK/code-$TS.bundle" | cut -f1), $(git rev-list --count HEAD 2>/dev/null) commits)"
fi

# ---- 3. the CONFIG (no secrets — .env files are gitignored) ---------------
if [ -d .git ]; then
  ( cd /opt/trading-agent && git archive --format=tar.gz \
      -o "$BK/config-$TS.tar.gz" HEAD config .gitignore 2>/dev/null ) \
    && echo "config      : $BK/config-$TS.tar.gz"
fi

# ---- 4. rotate: keep 14 locally ------------------------------------------
ls -1t "$BK"/trading-*.tar.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
ls -1t "$BK"/code-*.bundle   2>/dev/null | tail -n +8  | xargs -r rm -f
ls -1t "$BK"/config-*.tar.gz 2>/dev/null | tail -n +8  | xargs -r rm -f

# ---- 5. OPTIONAL off-box replication -------------------------------------
# Without this, every artefact above is still on the same disk it protects.
if [ "${1:-}" = "--offbox" ] && [ -n "${2:-}" ]; then
  rsync -az --timeout=60 "$BK/" "$2:/backups/trading-agent/" \
    && echo "off-box     : replicated to $2" \
    || echo "off-box     : FAILED (backups remain local only)"
fi

echo "backup done: $(ls -1 "$BK" | wc -l) artefacts in $BK"
