# Trading VPS — Ops Runbook

Quick reference for monitoring the adaptive trading system. All paths are on the VPS.

---

## 1. Is everything running?

```bash
pm2 status
```

Healthy = these are `online` (or `waiting restart` for cron jobs):

| Process | Expected status | What it is |
|---|---|---|
| `hermes-gateway` | online | Telegram + cron gateway |
| `watchdog` | online | continuous signal scanner + auto-trader |
| `dashboard` | online | dashboard API |
| `adaptive-engine` | **waiting restart** | hourly paper trade (cron) |
| `weekly-regime-refit` | **waiting restart** | Sunday edge re-fit (cron) |

`waiting restart` is NORMAL for cron jobs — they run once, exit, wait for the next tick.

---

## 2. The three things to check daily

### A. Watchdog health (circuit breaker)

```bash
pm2 logs watchdog --lines 15
```

Look for:
- ✅ `signals=N trades=0 circuit=False` → healthy
- ❌ `circuit_open=True` or `failure #N: ...` → something is wrong

### B. Adaptive engine state (what regime are we in?)

```bash
cat /opt/trading-agent/data/adaptive-state.json | python3 -m json.tool
```

Key fields:
- `regime`: `TREND+HIVOL` / `DOWNTREND+HIVOL` / `RANGE+HIVOL` = tradeable; `*LOVOL` = stand down (cash)
- `budget`: paper balance (starts $500)
- `open_positions` / `closed_trades`: current trades + history

### C. Live balance (real money)

```bash
cd /opt/trading-agent && PYTHONPATH=src ./venv/bin/python -c "from trading.exchange.live_client import live_balances; print(live_balances())"
```

---

## 3. Understanding the strategy (what "signal" means)

The adaptive engine only trades when ALL of these are true (BTC, 30m):
1. **Regime = high-vol** (market is moving)
2. **Trailing drawdown ≤ −2%** (a real dip)
3. **≥3 consecutive red bars** (capitulation flush)
4. **RSI ≤ 30** (deeply oversold)

Validated: ~70% win rate, +0.44% EV/trade, 2.92 profit factor.

**If it's been quiet for days — that's correct.** RSI ≤ 30 is rare by design. Cash is a position.

---

## 4. Reading the trade logs

```bash
# Closed paper trades (win/loss)
cat /opt/trading-agent/data/adaptive-state.json | python3 -c "import json,sys; d=json.load(sys.stdin); [print(t['regime'], t['pnl'], t['reason'], t['ts_utc'][:16]) for t in d['closed_trades']]"

# Full engine output
cat /opt/trading-agent/logs/adaptive-engine.log
```

---

## 5. Common situations

| Symptom | Meaning | Action |
|---|---|---|
| `regime=RANGE+LOVOL` for days | Calm market | Nothing — correct behavior |
| `failure #N: ...` in watchdog | API/network issue | Check live balance command; restart `pm2 restart watchdog` |
| `budget` dropping steadily | Strategy losing (paper) | Review closed trades; check if regime edge decayed |
| High `↺` (restarts) on watchdog | Crash loop | `pm2 logs watchdog --err` for the traceback |

---

## 6. Restart / recover

```bash
# Restart the scanner
pm2 restart watchdog

# Restart the adaptive engine (runs once, then waits)
pm2 restart adaptive-engine

# Full restart of everything
pm2 restart all
```

After a reboot, PM2 auto-resumes (config saved via `pm2 save`).

---

## 7. Weekly rhythm (the "adapt" loop)

Every **Sunday 09:15**, `weekly-regime-refit` re-measures each regime's win rate/EV
from recent data and writes `data/regime-edges.json`. The engine picks it up
automatically on its next hourly cycle — if an edge decays, sizing drops with it.

Check the result:
```bash
cat /opt/trading-agent/data/regime-edges.json
```

---

## 8. Key file locations

| What | Where |
|---|---|
| Adaptive engine | `/opt/trading-agent/scripts/adaptive_engine.py` |
| Regime refit (weekly) | `/opt/trading-agent/scripts/weekly_regime_refit.py` |
| PM2 config (engine + refit) | `/opt/trading-agent/adaptive.ecosystem.config.js` |
| PM2 config (full stack) | `/opt/trading-agent/deploy/ecosystem.config.js` |
| Architecture doc | `/opt/trading-agent/docs/adaptive-framework.md` |
| Engine state | `/opt/trading-agent/data/adaptive-state.json` |
| Regime edges | `/opt/trading-agent/data/regime-edges.json` |
| Live client | `/opt/trading-agent/src/trading/exchange/live_client.py` |

---

## 9. Golden rules (the mindset, encoded)

1. **Expectancy > win rate** — profit factor is what pays.
2. **Patience is alpha** — not trading is a position.
3. **Regime before rule** — never trade without knowing the mode.
4. **Size to edge** — Kelly-capped, never by confidence.
5. **Survive first** — a strategy that liquidates isn't a strategy.
6. **Trust walk-forward, not backtest** — one lucky split lies.
7. **Small size is the superpower** — funds can't do what a $500 account can.
