# Adaptive Trading Framework — Architecture & Workflow

> Status: **DESIGN** — informed by validated data, build in progress.
> Core mindset: *the market is chaotic (structured, not random). A fixed strategy
> only wins in some states. Outsmarting the system = detect the state first,
> then apply the right rule, sized to the edge — with the discipline and patience
> big firms can't afford at our size.*

---

## 1. The core thesis (validated, not assumed)

**Market is chaotic, not random.** Proof from our 7.2yr / 3.6yr data:
- Same signal (dip + highvol + 3red + RSI≤30) produced 40% → 100% win rate
  across different periods. That's regime-dependence, not noise.
- Regime separation test (regime_proof.py): the capitulation signal fires ONLY
  in high-vol regimes, and trend-direction tilts EV meaningfully.

**Our edge (what big firms can't/won't do):**
- Trade tiny size (no slippage problem)
- Sit in cash for weeks (no mandate to stay invested)
- Exploit rare signals too small for funds to bother with
- Iterate fast (no compliance/committee)

**What we explicitly do NOT try:** out-speed, out-data, or out-compute Citadel.
We win on *discipline, patience, and regime-awareness* — not on raw prediction.

---

## 2. Architecture (4 layers)

```
┌────────────────────────────────────────────────────────────┐
│ LAYER 1 — REGIME DETECTOR (realtime, every 30m bar)        │
│   Input: 90-bar slope, 14-bar vol, ATR ratio               │
│   Output: market mode tag                                  │
│     • TREND + HIVOL   (best for capitulation: +0.62% EV)   │
│     • DOWNTREND + HIVOL (+0.46% EV)                        │
│     • RANGE + HIVOL   (+0.26% EV)                          │
│     • * LOWVOL        (NO TRADE — no edge, stand down)     │
├────────────────────────────────────────────────────────────┤
│ LAYER 2 — STRATEGY SELECTOR                                │
│   Maps regime → concrete entry rule:                       │
│     HIVOL + capitulation signal → Capitulation Catch (BTC) │
│     LOWVOL                        → cash (no position)     │
│   (Future: DOWNTREND → knife-catch variant; TREND →        │
│    momentum continuation. Only added if walk-forward        │
│    proves each earns its keep.)                            │
├────────────────────────────────────────────────────────────┤
│ LAYER 3 — POSITION SIZER (Kelly-capped by EDGE, not hope)  │
│   size = f(expectancy, win-rate, R:R), hard-capped         │
│   Risk 1-2% equity per trade. Leverage only if R:R + EV     │
│   justify (never >3x; 15x is rejected — liq distance -6%). │
├────────────────────────────────────────────────────────────┤
│ LAYER 4 — LEARNER (weekly + monthly, walk-forward)         │
│   Re-fit each regime-strategy on rolling windows.          │
│   Rotate OUT stale edges; promote new ones only if OOS.    │
│   Weekly: win-rate/EV drift check + param re-tune.         │
│   Monthly: full walk-forward re-validation + strategy audit.│
└────────────────────────────────────────────────────────────┘
```

---

## 3. Cadences (the "must-have" heartbeat)

| Cadence | Job | Owner |
|---|---|---|
| **30m (realtime)** | Regime detect + capitulation signal scan + paper trade | `capitulation_catch.py` (PM2, hourly check) |
| **Hourly** | Position management: TP/SL/time-stop | same |
| **Daily** | Data-health check, balance-drift, signal report | `daily_signal_report.py` (exists) |
| **Weekly (HIGH PRIORITY)** | Walk-forward re-fit, win-rate/EV drift, param rotation | `weekly_learning.py` (exists, extend) |
| **Monthly** | Full re-validation, strategy audit, regime-edge re-confirm | new `monthly_audit.py` |

---

## 4. Data contracts

- **Regime tag:** `TREND|DOWNTREND|RANGE` × `HIVOL|LOVOL` (6 possible, 3 currently tradeable)
- **Signal record:** symbol, regime, dd, atr_ratio, red_bars, rsi, size, tp, sl, edge_ev
- **Trade log:** entry/exit/regime/pnl/reason → feeds Layer 4 learner
- **Source of truth:** `data/trading.db` (candles) + JSON state files

---

## 5. Validation discipline (non-negotiable)

Every strategy change must clear ALL gates:
1. **Walk-forward** (rolling windows, not one split) — kills overfit
2. **Out-of-sample** — new params tested on held-out data
3. **Sample size** — n ≥ 50 OOS before trusting any win-rate claim
4. **Profit factor** — must be > 1.5 (win-rate alone is a trap)
5. **Paper-first** — 30+ live-paper trades confirming edge before any real money

The 84%→66% collapse (accuracy_hunt.py → walk_forward_confirm.py) is our
permanent cautionary tale: a single split lies.

---

## 6. What we know works (validated baselines)

- **Capitulation Catch (BTC, 30m):** dip-2% + highvol + 3red + RSI≤30, hold 6h
  → 70.2% WR, +0.44% EV, 2.92 profit factor (walk-forward stable)
- **Alts are negative expectancy** (SOL PF 0.93) — BTC only
- **1h scalping is a coin flip** — abandoned
- **Leverage ceiling ~5x; 15x = liquidation at -6%** — rejected

---

## 7. Build phases (in progress)

- [x] **Step 4 (prove it):** regime separation test → confirmed vol is the
      dominant split; trend-direction adds marginal EV
- [ ] **Step 3 (architecture):** this document
- [ ] **Step 1 (framework):** regime detector + strategy selector + sizer wired
      into a single adaptive engine
- [ ] **Step 2 (regime filter):** apply regime gate on top of capitulation catch
- [ ] **Validate:** walk-forward each layer before the next

---

## 8. The mindset (operating principles)

1. **Expectancy > win rate.** 60% with 2:1 beats 80% with 1:1.
2. **Patience is alpha.** Not trading is a position. Cash is a strategy.
3. **Regime before rule.** Never apply a strategy without knowing the mode.
4. **Size to edge, not to confidence.** Kelly, capped.
5. **Survive first, profit second.** A strategy that liquidates isn't a strategy.
6. **Trust the walk-forward, not the backtest.** One lucky split lies.
7. **Small size is our superpower.** Funds can't do what a $500 account can.
