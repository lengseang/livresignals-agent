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
│    ENFORCED: research_loop gates every "NEW BEST" on             │
│    beating buy&hold (fail-safe on missing benchmark).            │
│                                                                 │
│ 4. ALWAYS include fees (0.144% round trip).                     │
│    Our gross edge is ~0; fees were 100% of the loss.            │
│                                                                 │
│ 5. ALWAYS ask "what regime was the test period?"                │
│    Long-only tested in a -60% market looks broken. It isn't.    │
│                                                                 │
│ 6. ALWAYS collapse duplicate WRITES before measuring.           │
│    One physical dip is booked once per timeframe (5m/15m/30m):  │
│    scripts/paper_book.collapse_multitf() removes ~500 phantom   │
│    rows (17%) that flattered the book. A time-based dedupe key  │
│    CANNOT see them — they differ in ts_utc. Key on the economic │
│    fingerprint (exact entry + exact pnl), not on time.          │
│                                                                 │
│ 7. ALWAYS compare ratios with a TOLERANCE.                      │
│    The dominant design is an exact 2:1, and abs(tp-e)/abs(e-sl) │
│    floats a hair either side of 2.0. A raw `rr >= 2.0` split   │
│    ONE design into "winning population" (119) and "below 2.0,   │
│    skip it" (56) — and the verdict note ROUNDED 1.996 up to    │
│    "2.00 below 2.0". Use _rr_meets_good/_rr_text, not `>=`.     │
│                                                                 │
│ 8. ALWAYS re-derive a threshold on the current book.            │
│    RR_GOOD=2.0 was true when it was measured. The 2.0-2.1 band  │
│    is now net -11.6 over 240 trades; the first paying band is   │
│    2.40. A stale threshold labels losing trades "variance".     │
│    See docs/proposals/rr-boundary-tolerance-2026-10-18.md.      │
│                                                                 │
│ 9. NEVER QUOTE A MEASUREMENT; RE-DERIVE IT AT WRITE TIME.       │
│    The retraction of "RR<2 loses" was supported with            │
│    "+9.95 first half / -1.20 second" — a fact about the ledger  │
│     on 2026-10-18, written into improve() and write_rollup()    │
│    as a STRING. One day / 24 lessons later the same computation │
│    returns +11.16 / +0.06: the second half is now POSITIVE.     │
│    rr_split() derives it per run and refuses when a clean cut   │
│    would fall inside one cluster of simultaneous fills;         │
│    rr_split_text() states the SHAPE, not two bare numbers.      │
│    See docs/proposals/lesson-split-freshness-2026-10-19.md.     │
│                                                                 │
│ 10. A MONITOR PUBLISHES ONLY FROM ITS ENTRYPOINT.               │
│    check_data_health(db_path) wrote the GLOBAL                  │
│    reports/data-health.json on every call — so the unit tests,  │
│    each on an empty temp DB, rewrote the live report. It read   │
│    `ok:false, 15x "no data"` about a store which that same      │
│    function, run on the real DB, called healthy (9.56M candles, │
│    60 symbol×tf groups, 0 duplicates). The report now names the │
│    DB it judged (`"db": ...`) and only the CLI publishes.       │
│    Corollary: every _check_* must be able to FAIL —             │
│    storage_check returned a dict nobody read, and an `ok` flag  │
│    set once and never flipped passed its own tests' intent.     │
│    See docs/proposals/data-health-reported-the-wrong-db-*.md.   │
│                                                                 │
│ 11. A TEST THAT PINS TOMORROW'S DATA GOES RED FOR NO DEFECT.    │
│    test_the_solo_challenger... asserted `live_fragile is False` │
│    on the LIVE cache. True on 10-19 (live 13.7% inside          │
│    [3.58,22.59]); false after the 01:00 refresh (live 25.11%    │
│    ABOVE its ceiling, crowned config moved to sma_r=100).       │
│    Assert the INVARIANT (`live_fragile == median outside band`) │
│    on live data; pin the historical numbers with a literal      │
│    fixture. A monitor carousel that goes red for no reason      │
│    trains the reader to ignore red.                             │
│    Corollary: a derived verdict must not be narrated with a      │
│    hardcoded direction — the line said "below its own floor"     │
│    while the data was ABOVE the ceiling.                         │
│                                                                  │
│ 12. A VERDICT MUST STATE THE WINDOW IT WAS MEASURED ON.          │
│    paper_learner printed "best found … net_ev=+0.184% n=907"     │
│    daily and it read as a fact about TODAY. It was a fact        │
│    about 2023-01-01..2026-08-15: the grid is scored on           │
│    live_fine.json / alts_fine.json, both frozen 65 days ago,     │
│    while the DB held candles to today. Downstream                │
│    (improvement-report → self_improve → night_tuner) reasoned    │
│    about "the sharp-dip entry is negative-EV" from it. The       │
│    adopted config fired ZERO times in the last 90 days — the     │
│    verdict described a market that no longer existed, and no     │
│    reader could tell. The run now prints "INPUT: … last          │
│    updated 65d ago (frozen history, NOT live)", derived from     │
│    the file mtime each run and keyed by the series actually      │
│    scored (not the raw key, which the 15m→BTC remap orphans).    │
│    Stale INPUT is not a small caveat: it voids every             │
│    downstream inference built on the line.                       │
│    See docs/proposals/learner-scored-frozen-data-2026-10-20.md.  │
│                                                                 │
│ 13. A PERSISTED NET IS A MEASUREMENT, NOT A FORMULA.             │
│    paper_net.net_of trusted any recorded `pnl_net`. But it is a  │
│    NUMBER frozen at write time: 226 of 514 records were charged  │
│    the old 0.03% while the live round trip was 0.1545% — one      │
│    ledger, three different rates in the `cost` column. The book  │
│    read +$3.04 net when the honest figure was +$1.63 (46% over). │
│    Invisible before 2026-10-11 because reader and writer shared  │
│    the SAME stale constant; the fix corrected only the writer,   │
│    and every record already written stayed cheap and kept the    │
│    recorded field's authority. Now a `cost` is believed only if  │
│    it is >= 50% of the live rate; a cheaper non-zero charge is   │
│    re-costed from gross; cost 0.0 w/ pnl_net==pnl stays (that is │
│    a deliberate uncharged book, PAPER_CHARGE_COST=0).            │
│    Same rule as #9, applied to a stored number rather than a     │
│    comment: re-derive at READ time. See                        │
│    tests/test_paper_net_stale_cost.py.                          │
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
