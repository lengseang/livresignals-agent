# HERTRADE — SYSTEM MAP
### The whole tree, one page. Read this before changing anything.

> Purpose: understand the machine before improving it. This is a *living*
> document — update it whenever the system changes.

---

## 0. THE BIG PICTURE — three nested loops

```
┌──────────────────────────────────────────────────────────────────────┐
│                    LOOP 3: THE EDGE-HUNT LOOP                        │
│                    (weeks) — "is our premise still true?"            │
│                                                                      │
│   hypothesise ──► backtest ──► IS/OOS validate ──► REJECT or DEPLOY  │
│        ▲                                              │              │
│        │                                              ▼              │
│        └──── measure live results ◄──── run in demo ──┘              │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │              LOOP 2: THE ADAPTATION LOOP                       │  │
│  │              (daily) — "are the knobs right?"                  │  │
│  │                                                                │  │
│  │   night_tuner ──► adjust risk knobs ──► self_improve ──►        │  │
│  │        ▲                                disable losers          │  │
│  │        │                                       │               │  │
│  │        └──── measure live WR/PnL ◄─────────────┘               │  │
│  │                                                                │  │
│  │  ┌──────────────────────────────────────────────────────────┐  │  │
│  │  │          LOOP 1: THE TRADING LOOP                        │  │  │
│  │  │          (minutes) — "execute the plan"                  │  │  │
│  │  │                                                          │  │  │
│  │  │   market data ─► signal ─► GATES ─► size ─► order ─►     │  │  │
│  │  │                                         ▲        │       │  │  │
│  │  │                                         │        ▼       │  │  │
│  │  │                          position mgmt ◄─ fills/track   │  │  │
│  │  └──────────────────────────────────────────────────────────┘  │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

**Key idea:** the inner loop makes money. The outer loops decide *whether the
inner loop deserves to exist*. Most traders only ever run Loop 1.

---

## 1. DATA LAYER — what flows in

```
┌───────────── SOURCES ─────────────┐
│                                   │
│  Binance REST   → candles, depth  │  spot      (real money)
│  Binance FUT    → funding, perps  │  futures   (real, small)
│  Bybit DEMO     → $100k sandbox   │  test venue
│  FRED (Fed)     → real yields,    │  MACRO layer
│                   dollar, CPI     │  (free, daily)
│  ForexFactory   → event calendar  │  EVENT layer
│  Yahoo / stooq  → gold history    │  (10y backtest)
│                                   │
└───────────────┬───────────────────┘
                ▼
┌───────────── STORES ──────────────────────────────────┐
│  data/trading.db        SQLite ~23MB (candles, state) │
│  data/*.json            state files (positions, logs) │
│  data/fred/*.csv        macro series (offline backup) │
│  data/ff-seed.json      last known-good calendar      │
│  logs/*.log             cron output (the audit trail) │
└───────────────────────────────────────────────────────┘
```

---

## 2. SIGNAL LAYER — how a decision is born

```
                     market data (1d / 4h / 15m)
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
   REGIME                 TREND                 MACRO/EVENT
   BTC > SMA200?          coin > SMA50?          real yields?
   (bull/bear)            (direction)            dollar?  events?
        │                     │                     │
        └─────────────────────┼─────────────────────┘
                              ▼
                    ╔═════════════════════╗
                    ║   GATE STACK        ║   ALL must pass
                    ╠═════════════════════╣
                    ║ 1. regime  BTC>SMA200║
                    ║ 2. trend   px>SMA50 ║
                    ║ 3. macro   not        ║
                    ║            HEADWIND   ║
                    ║ 4. events  no blackout║
                    ║ 5. breadth risk-off?  ║
                    ║ 6. disable-list       ║
                    ╚═════════╤═══════════╝
                              ▼
                   conviction / vol-scaled SIZING
                              ▼
                         ORDER
```

**Every gate exists because a previous loss proved it was needed.**

---

## 3. EXECUTION LAYER — where money actually moves

```
┌────────────── REAL MONEY ──────────────┐  ┌──────── SANDBOX ────────┐
│  Binance SPOT  ~$12.71                 │  │  Bybit DEMO  ~$100k     │
│    trend_follower   (3 pos, 4h)        │  │   crypto + gold (XAUT)  │
│                                        │  │   every 4h              │
│  Binance FUTURES  ~$3.00               │  │                         │
│    lev_satellite   3x SOL, $3 cap      │  │  ↳ test new ideas here  │
│                                        │  │    for FREE             │
│  ⚠ no self-hosted edge proven yet      │  │                         │
└────────────────────────────────────────┘  └─────────────────────────┘
```

---

## 4. MEASUREMENT LAYER — the part that makes this different

```
┌─────────────────────────────────────────────────────────────────┐
│  /tmp/edge_hunt.py    batch 1: momentum, squeeze, reversion     │
│  /tmp/edge_batch2.py  batch 2: short-select, vol-scale, regime  │
│  /tmp/edge_synth.py   synthesis: regime-adaptive                │
│  /tmp/macro_gold.py   macro-gold correlation study (10y)        │
│  /tmp/gold_hf.py      high-frequency fee study                  │
│  /tmp/ruin.py         leverage ruin math                        │
│  /tmp/scale_math.py   can $15 reach $5/month?                   │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼  RULES (learned the hard way — DO NOT VIOLATE)
┌─────────────────────────────────────────────────────────────────┐
│ 1. ALWAYS simulate the SHARED ACCOUNT.                          │
│    Averaging per-coin curves inflated "+12.6% OOS" from a       │
│    true -5.7%. This rule exists because we were fooled once.    │
│                                                                 │
│ 2. ALWAYS split IS/OOS. A result that only exists in-sample     │
│    is noise. Momentum showed +116% IS → -80% OOS.               │
│                                                                 │
│ 3. ALWAYS compare to BTC buy&hold.                              │
│    If you don't beat it, you have no edge.                      │
│                                                                 │
│ 4. ALWAYS include fees (0.144% round trip).                     │
│    Our gross edge is ~0; fees were 100% of the loss.            │
│                                                                 │
│ 5. ALWAYS ask "what regime was the test period?"                │
│    Long-only tested in a -60% market looks broken. It isn't.    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 5. WHAT IS PROVEN (and what is not)

```
✅ PROVEN GOOD
   • Regime-adaptive L/S:  OOS +40.8%, DD -21.9%  (bear market!)
   • Vol-scaled sizing:    +2.8% IS / +7.2% OOS, DD -20.1%
   • Macro drives gold:    corr -0.47 (real yields), -0.46 (dollar)
   • Event blackout:       standing down around FOMC is free safety
   • Fee math:             more trades = more loss, linearly

❌ PROVEN BAD
   • Cross-sectional momentum:  +116% IS → -80.6% OOS
   • High-frequency/scalping:   -30% to -57% (fee death)
   • Dip-buying / accuracy:     66% win rate still LOSES
   • Leverage without edge:     10x = 96.6% ruin

⚠ UNKNOWN / UNTESTED
   • Long-side selection (weak in tests — the alpha is in SHORTS)
   • Funding-aware short selection
   • Multi-timeframe confirmation
   • Whether the OOS edge survives a BULL market
```

---

## 6. THE ADAPTATION LOOP — how this document stays true

```
        ┌──────────────────────────────────────────┐
        │                                          │
        ▼                                          │
   OBSERVE          a loss, a surprise, a          │
   (loop 3)         strategy that decayed         │
        │                                          │
        ▼                                          │
   HYPOTHESISE      "the missing piece is X"      │
        │                                          │
        ▼                                          │
   TEST             shared-account, IS/OOS,       │
                    fees, vs BTC benchmark        │
        │                                          │
        ▼                                          │
   VERDICT          ┌─ reject ─────────────────┐  │
                    │  (record it! negative     │  │
                    │   knowledge is knowledge) │──┤
                    └───────────────────────────┘  │
                    ┌─ deploy to DEMO ─────────┐  │
                    │  (never straight to real) │  │
                    └──────────┬────────────────┘  │
                               ▼                   │
                          MEASURE LIVE ────────────┘
                          (does reality match?)
```

---

## 7. HARD RULES (guard rails that exist for a reason)

| Rule | Why it exists |
|---|---|
| Demo before real | We've been wrong before; demo costs nothing |
| Max 3 real positions | $15 account; concentration kills |
| $3 max loss on satellite | Bounded downside on leverage |
| Never withdraw enabled on keys | Exchange-level protection |
| No unbounded leverage | 10x = 96.6% ruin, proven |
| Report numbers straight | The day we shade results is the day this is worthless |

---

## 8. CURRENT SCOREBOARD (2026-09-13)

```
REAL MONEY          ~$15.71   (realized -$0.29 over 30 trades)
  spot              ~$12.71   NEAR, ARB, DOGE + dust
  futures           ~$3.00    3x SOL satellite, -$0.10

BEST PROVEN EDGE    regime-adaptive L/S (+29.7% full, +40.8% OOS, DD -21.9%)
                    → NOT YET DEPLOYED ANYWHERE

SANDBOX             Bybit demo ~$100k, 4 positions, macro+event gated

SYSTEM HEALTH       17/17 bots running after the PYTHONPATH fix
```

---

## 9. NEXT (in priority order)

1. **Deploy the regime-adaptive strategy to the DEMO** — validate live
2. **Test the long-side properly** — the alpha is in shorts; fix longs
3. **Combine: BTC hold in bull + regime-short in bear**
4. **Then, only if live demo matches backtest → real money**
