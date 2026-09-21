#!/usr/bin/env bash
# Fetch full Binance USDT-M futures funding history for a symbol.
# Uses direct-IP DNS workaround (ISP blocks DNS for fapi.binance.com but the
# endpoint itself is public and reachable).
# Usage: fetch_funding.sh BTCUSDT <start_ms> <output.json>
set -euo pipefail
SYM="${1:-BTCUSDT}"
START="${2:-0}"
OUT="${3:-reports/p4/funding-history.json}"

IP=$(nslookup fapi.binance.com 8.8.8.8 2>/dev/null | grep -A1 "Name:" | grep Address | awk '{print $2}' | head -1)
[ -n "$IP" ] || { echo "DNS resolve failed"; exit 1; }

ALL=reports/p4/.funding-all.tmp
: > "$ALL"
CURSOR="$START"
COUNT=0
for i in $(seq 1 40); do
  RESP=$(curl -s --max-time 20 --resolve "fapi.binance.com:443:$IP" \
    "https://fapi.binance.com/fapi/v1/fundingRate?symbol=$SYM&limit=500&startTime=$CURSOR")
  N=$(echo "$RESP" | python -c "import json,sys; d=json.load(sys.stdin); print(len(d))" 2>/dev/null || echo 0)
  [ "$N" -eq 0 ] && break
  echo "$RESP" | python -c "import json,sys; [print(json.dumps(x)) for x in json.load(sys.stdin)]" >> "$ALL"
  LAST=$(echo "$RESP" | python -c "import json,sys; d=json.load(sys.stdin); print(d[-1]['fundingTime'])" 2>/dev/null || true)
  [ -n "$LAST" ] && CURSOR=$((LAST + 1))
  COUNT=$((COUNT + N))
  echo "batch $i: $N records (total $COUNT, cursor $CURSOR)" >&2
  [ "$N" -lt 500 ] && break
done
python -c "
import json
recs = [json.loads(l) for l in open('$ALL') if l.strip()]
uniq = {}
for r in recs:
    uniq[r['fundingTime']] = r  # dedup by time
out = sorted(uniq.values(), key=lambda r: r['fundingTime'])
json.dump(out, open('$OUT','w'), indent=1)
print('wrote %d unique records -> %s' % (len(out), '$OUT'))
"