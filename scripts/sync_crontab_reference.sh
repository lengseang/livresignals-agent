#!/usr/bin/env bash
# Regenerate config/crontab.trade from the live crontab.
# The tracked file is a REFERENCE SNAPSHOT so the schedule survives a box
# rebuild. The live crontab is the source of truth; nothing auto-installs this.
set -uo pipefail
cd /opt/trading-agent || exit 1

LIVE=/tmp/crontab.live
crontab -l > "$LIVE" 2>/dev/null || { echo "crontab -l failed"; exit 1; }

JOBS=$(grep -cvE '^[[:space:]]*#|^[[:space:]]*$' "$LIVE" || true)

{
  echo "# ============================================================"
  echo "#  config/crontab.trade — REFERENCE COPY of the live crontab."
  echo "#"
  echo "#  This file is NOT installed by anything automatically."
  echo "#  It is a versioned snapshot so the schedule survives a"
  echo "#  box rebuild. The live crontab is the source of truth."
  echo "#"
  echo "#  Verify drift with:"
  echo "#    diff <(grep -vE '^[[:space:]]*#|^[[:space:]]*\$' config/crontab.trade) \\"
  echo "#         <(crontab -l | grep -vE '^[[:space:]]*#|^[[:space:]]*\$')"
  echo "#"
  echo "#  Regenerated: $(date -u +%Y-%m-%dT%H:%M:%SZ) from 'crontab -l'"
  echo "#  Jobs: $JOBS"
  echo "# ============================================================"
  echo
  cat "$LIVE"
} > config/crontab.trade

echo "wrote config/crontab.trade ($JOBS jobs)"
