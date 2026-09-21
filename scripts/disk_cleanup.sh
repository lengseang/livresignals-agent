#!/usr/bin/env bash
# DISK CLEANUP — safe, verified, frees ~600MB
#
# WHY THIS EXISTS: the VPS hit 94% disk (431MB free). The trading system is
# one large log file away from failing. This script is the fix.
#
# SAFETY: every path below was VERIFIED as unreferenced or regenerable.
#   - full_sweep.json : one-off parameter-sweep output, referenced NOWHERE
#   - caches          : pip/uv/npm build caches, always regenerable
#   - backups         : keeps the 3 most recent, prunes older
#   - logs            : TRUNCATES (keeps last 100KB), never deletes
#
# DELIBERATELY NOT REMOVED:
#   - sklearn / scipy : LIVE DEPENDENCIES of src/trading/signals/scorer.py
#                       and scripts/paper_learner.py. Removing them would
#                       break the signal scorer. (Nearly did - caught it.)
#   - data/trading.db : the trade database
#   - any live/paper state files
#
# Run:  bash /opt/trading-agent/scripts/disk_cleanup.sh

set -uo pipefail
cd /opt/trading-agent || exit 1

echo "=== BEFORE ==="
df -h / | tail -1
echo

echo "--- 1. unreferenced one-off sweep output (162M) ---"
if [ -f data/full_sweep.json ]; then
  ls -lh data/full_sweep.json
  rm -f data/full_sweep.json && echo "  ✅ deleted full_sweep.json (regenerable via research harness)"
else
  echo "  already gone"
fi
echo

echo "--- 2. build caches (pip/uv/npm) ---"
du -sh /home/ubuntu/.cache /home/ubuntu/.npm 2>/dev/null | sed 's/^/  /'
rm -rf /home/ubuntu/.cache/pip /home/ubuntu/.cache/uv \
       /home/ubuntu/.cache/pypoetry /home/ubuntu/.npm/_cacache 2>/dev/null
echo "  ✅ cleared (all regenerate on next install)"
echo

echo "--- 3. prune backups, keep 3 most recent ---"
echo "  before: $(ls backups/*.tar.gz 2>/dev/null | wc -l) backups"
ls -t backups/*.tar.gz 2>/dev/null | tail -n +4 | while read -r f; do
  rm -f "$f" && echo "  removed $(basename "$f")"
done
echo "  ✅ after: $(ls backups/*.tar.gz 2>/dev/null | wc -l) backups kept"
echo

echo "--- 4. truncate large logs (keep last 100KB) ---"
find logs -name "*.log" -size +200k 2>/dev/null | while read -r f; do
  sz=$(du -h "$f" | cut -f1)
  tail -c 100000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  echo "  truncated $f (was $sz)"
done
echo "  ✅ done"
echo

echo "--- 5. apt cache + system logs (needs sudo) ---"
if sudo -n true 2>/dev/null; then
  sudo apt-get clean
  sudo journalctl --vacuum-size=50M 2>/dev/null | tail -1
  echo "  ✅ apt + journal cleaned"
else
  echo "  ⚠️  no passwordless sudo - run these yourself if you want ~250MB more:"
  echo "       sudo apt-get clean"
  echo "       sudo journalctl --vacuum-size=50M"
fi
echo

echo "=== AFTER ==="
df -h / | tail -1
echo
echo "Target: under 80% used. If still high, check: du -sh /home/ubuntu/* | sort -rh | head"
