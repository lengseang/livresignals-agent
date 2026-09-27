#!/usr/bin/env bash
# P-A daily paper report — runs accumulation paper mode, sends summary to Telegram.
# Intended as a cron job. Routes to the REPORTS topic via tg-send.sh.
set -uo pipefail

# Resolve the repo root portably. This used to be `cd /z/Hermes/home/trading-agent
# || exit 1` — a Windows-era path that does not exist on the Linux VPS, so the
# script aborted on line 5 every single run and NEVER produced a report.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

# Use the project venv; a bare `python` is not guaranteed under cron's PATH.
PY="$ROOT/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

REPORT=$(PYTHONPATH=src "$PY" -m trading.allocation.paper 2>/dev/null | "$PY" -c "
import json, sys
d = json.load(sys.stdin)
r = d['result']
cfg = d['config']
print(f'BTC accumulated: {r[\"btc_accumulated\"]:.6f}')
print(f'Cash invested: \$ {r[\"cash_invested_usdt\"]:,.2f}')
print(f'Final value: \$ {r[\"final_value_usdt\"]:,.2f} ({r[\"return_pct\"]:.1f}% return)')
print(f'Entries: {r[\"entries\"]} (monthly, {cfg[\"interval_d\"]}d)')
print(f'Scaling: dip {cfg[\"dip_x\"]}x / ext {cfg[\"ext_x\"]}x, dd-thr {cfg[\"dd_thr\"]:.0%}')
")

# Advance the live-forward wallet with any NEW candles, then report status.
# NOTE: this is a STATE MUTATION. It used to be `>/dev/null 2>&1 || true`, which
# meant a failure here silently produced a report full of STALE numbers that
# looked completely normal — the worst kind of wrong. We now surface the error
# in the report body instead of hiding it.
ADVANCE_OUT=$(PYTHONPATH=src "$PY" -m trading.allocation.forward advance 2>&1) || \
  ADVANCE_ERR="⚠️ forward advance FAILED — figures below may be stale
$(echo "$ADVANCE_OUT" | tail -3)"

FORWARD=$(PYTHONPATH=src "$PY" -m trading.allocation.forward status 2>/dev/null | "$PY" -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(f'Forward wallet: \$ {d[\"portfolio_value_usdt\"]:,.2f} ({d[\"entries\"]} entries, {d[\"btc\"]:.6f} BTC, price \$ {d[\"current_price\"]:,.0f})')
except Exception:
    print('forward tracker: n/a')
" || echo "forward tracker: n/a")

# Live testnet PnL snapshot (append-only history + JSON report)
PNL=$(PYTHONPATH=src "$PY" -m trading.allocation.pnl 2>/dev/null | "$PY" -c "
import json, sys
try:
    d = json.load(sys.stdin)
    p = d['portfolio']
    print(f'PnL: \$ {p[\"unrealized_pnl\"]:+,.2f} ({p[\"unrealized_pnl_pct\"]:+.2f}%) | cost \$ {p[\"cost_basis\"]:,.2f} -> value \$ {p[\"market_value\"]:,.2f}')
except Exception:
    print('pnl tracker: n/a')
" || echo "pnl tracker: n/a")

STATUS=$(cd "$ROOT" && PYTHONPATH=src "$PY" -m trading.data.health 2>/dev/null | "$PY" -c "import json,sys; d=json.load(sys.stdin); print('DATA_HEALTH_OK' if d.get('ok') else 'DATA_HEALTH_ISSUES: ' + '; '.join(d.get('problems',[])[:3]))" || echo "health check failed")

MSG="📈 P-A Paper Report
${ADVANCE_ERR:+$ADVANCE_ERR

}$REPORT

📊 $FORWARD

💵 $PNL

🩺 $STATUS
— Hermes overnight build"

# Route to the REPORTS topic via tg-send.sh (config/topic-routing.json).
# FIXED: bare `--to telegram` resolves to the DM/home channel and ignored the
# topic map, so the daily paper report never reached the reports topic.
/opt/trading-agent/scripts/tg-send.sh reports "📈 P-A Paper Report" "$MSG"