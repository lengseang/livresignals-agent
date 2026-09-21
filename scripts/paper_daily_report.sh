#!/usr/bin/env bash
# P-A daily paper report — runs accumulation paper mode, sends summary to Telegram.
# Intended as a cron job. Uses hermes send (no agent loop, no LLM).
set -uo pipefail
cd /z/Hermes/home/trading-agent || exit 1

REPORT=$(PYTHONPATH=src python -m trading.allocation.paper 2>/dev/null | python -c "
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

# Advance the live-forward wallet with any NEW candles, then report status
PYTHONPATH=src python -m trading.allocation.forward advance >/dev/null 2>&1 || true

FORWARD=$(PYTHONPATH=src python -m trading.allocation.forward status 2>/dev/null | python -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(f'Forward wallet: \$ {d[\"portfolio_value_usdt\"]:,.2f} ({d[\"entries\"]} entries, {d[\"btc\"]:.6f} BTC, price \$ {d[\"current_price\"]:,.0f})')
except Exception:
    print('forward tracker: n/a')
" || echo "forward tracker: n/a")

# Live testnet PnL snapshot (append-only history + JSON report)
PNL=$(PYTHONPATH=src python -m trading.allocation.pnl 2>/dev/null | python -c "
import json, sys
try:
    d = json.load(sys.stdin)
    p = d['portfolio']
    print(f'PnL: \$ {p[\"unrealized_pnl\"]:+,.2f} ({p[\"unrealized_pnl_pct\"]:+.2f}%) | cost \$ {p[\"cost_basis\"]:,.2f} -> value \$ {p[\"market_value\"]:,.2f}')
except Exception:
    print('pnl tracker: n/a')
" || echo "pnl tracker: n/a")

STATUS=$(cd /z/Hermes/home/trading-agent && PYTHONPATH=src python -m trading.data.health 2>/dev/null | python -c "import json,sys; d=json.load(sys.stdin); print('DATA_HEALTH_OK' if d.get('ok') else 'DATA_HEALTH_ISSUES: ' + '; '.join(d.get('problems',[])[:3]))" || echo "health check failed")

MSG="📈 P-A Paper Report
$REPORT

📊 $FORWARD

💵 $PNL

🩺 $STATUS
— Hermes overnight build"

hermes send --to telegram --subject "📈 P-A Paper Report" "$MSG" 2>&1 | tail -1