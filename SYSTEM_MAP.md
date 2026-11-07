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

**A row in `candles` is not a closed bar.** `ingest_all.py` runs every 15 min and
requests raw candles *including the in-progress one*; the store is INSERT OR
IGNORE on (symbol, timeframe, open_time_utc), so the forming bar is written
early and never refreshed. Anything that prices an entry, an exit or a gate off
the newest row must first drop bars whose window has not ended
(`short_regime_paper.last_closed()` is the reference). See the 2026-10-24
journal entry.

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
│                                                                  │
│ 13. A PERSISTED NET IS A MEASUREMENT, NOT A FORMULA.             │
│    paper_net.net_of trusted any recorded `pnl_net`. But it is a  │
│    NUMBER frozen at write time: 226 of 514 records were charged  │
│    the old 0.03% while the live round trip was 0.1545% — one     │
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
│    comment: re-derive at READ time. See                          │
│    tests/test_paper_net_stale_cost.py.                           │
│                                                                  │
│ 14. A PER-EDGE VERDICT IS ABOUT AN EDGE THAT IS STILL TRADING.   │
│    improvement_loop quoted "Edge 'supertrend' DECAYING (net)     │
│    31.3% WR / 134 trades / -1.87 net — tighten its entry or      │
│    stand it down" on every run. Supertrend had been disabled and │
│    had not filled since 2026-09-08; same for bollinger (41d),    │
│    ema-cross (39d), momentum/breakout (38d), pullback (46d),     │
│    rsi2 (43d). Seven verdicts, zero of them actionable: an edge  │
│    that cannot open cannot be stood down, and its mean is a      │
│    historical population with no time bound — indistinguishable  │
│    on the card from a live one. Same class as #12 (window) and   │
│    #9 (re-derive at write time), applied to TIME. Now            │
│    paper_book.edge_liveness() stamps last_fill per edge; a stale │
│    edge gets one NOT LIVE note naming its last fill and draws    │
│    no tighten/stand-down verdict. The mirror holds on the write  │
│    side: self_improve REFUSES to promote a frozen record —       │
│    "proven" must mean proven while trading. An edge sharing      │
│    another's handoff (same tf/regime/entry/time) with no better  │
│    net or WR over n>=30 is a REDUNDANT COPY, not a second        │
│    experiment: it draws no independent verdict and cannot be     │
│    promoted on the parent's evidence (breakout/momentum ~ loose, │
│    rsi2 ~ supertrend). See tests/test_edge_liveness.py.          │
│                                                                  │
│ 15. AN AGGREGATE VERDICT OBEYS THE SAME POPULATION RULE AS THE   │
│     PER-EDGE ONES.                                               │
│     #14 fixed the per-edge verdicts; rule 3's profit factor was  │
│     the header above them and still pooled the WHOLE book. On    │
│     2026-10-22 it read "Profit factor 0.97 < 1.5 — raise TP:SL or│
│     tighten the stop", but the book decomposes as:               │
│       whole  n=2637  net -2.24  PF 0.97                          │
│       live   n=2365  net +3.44  PF 1.05  (loose, validated)      │
│       dead   n= 272  net -5.68  PF 0.47  (7 edges, >14d silent)  │
│     The 0.97 was manufactured by pooling the seven corpses the   │
│     SAME report named as HISTORY, and it read below 1.0 — "the   │
│     book loses before costs" — about a live book that pays.      │
│     `_split_live_pf` now splits the book before any aggregate is │
│     quoted, and the line states its cohort and what it excluded. │
│     Corollary (again): a GATE must be read at the BASIS it is    │
│     computed on. 1.5 was written for a GROSS book and has been   │
│     applied to a NET ratio since 2026-10-11 — unreachable, hence │
│     always on. PF_NET_FLOOR=1.02 is the net analogue. Same rule  │
│     as #9/#13 applied to a threshold rather than a measurement.  │
│     See docs/proposals/pf-live-population-2026-10-22.md.         │
│                                                                  │
│ 16. A FIX THAT CAN ONLY RUN WHERE THE DEFECT ISN'T IS NOT A FIX. │
│     #6's multi-tf collapse got a "cross-timeframe" clause on     │
│     2026-10-22. It was written as `if gap > window_s:` — a       │
│     FALLBACK for pairs FURTHER apart than the exact-fingerprint  │
│     window. But the real bookings are 0.5-1.2s apart, i.e.       │
│     INSIDE that 5s window, where the exact test still demanded   │
│     identical entry+pnl and rejected them (a tick apart). The    │
│     clause fired on 16 of 727 real pairs; the ledger still held  │
│     ~520 phantom rows a day after it was declared fixed, and its │
│     own three tests were red (the window, 20s, was even BELOW    │
│     the real max gap of 36.1s — set from a hand-written fixture, │
│     not from the book).                                          │
│     Now the two clauses are INDEPENDENT siblings and the window  │
│     is derived from a measured EMPTY BAND: bookings top out at   │
│     36.1s, the next honest re-entry is 357.7s (one scan), so 60s │
│     sits in the gap. Collapse went 525 -> 734 rows and it MOVES  │
│     VERDICTS: 'validated' net -0.13 -> -0.66 (now correctly      │
│     DECAYING). Lesson: when adding a clause to an existing key,  │
│     check it is REACHABLE in the regime the defect lives in, and │
│     set thresholds from the MEASURED distribution — a fixture    │
│     you wrote yourself encodes your own wrong assumption (#11).  │
│     tests/test_paper_book_multitf.py                               │
│     ::test_cross_tf_clause_reachable_INSIDE_the_exact_window     │
│                                                                  │
│ 17. AN AGGREGATE NEEDS A WINDOW AS WELL AS A COHORT.             │
│     #15 fixed the aggregate's COHORT (live edges, not corpses)  │
│     and left its PERIOD unbounded. On 2026-10-23 the live       │
│     cohort read PF 1.03 net +2.20 over n=2183 — 0.01 above the  │
│     1.02 floor — so rule 3 stayed SILENT. That mean was         │
│     2026-09 paying 1.17/+6.94 plus 2026-10 paying 0.82/-4.81;   │
│     the last 28d were 0.83/-6.26 and the weekly series was      │
│     still falling (1.53 1.27 0.75 0.75 1.20 0.50). The older    │
│     the book, the longer one good month hides a bad one: a      │
│     lifetime mean cannot answer "is it paying NOW?", which is   │
│     the only question exit tuning asks. `_recent_live_pf` does  │
│     it over PF_WINDOW_DAYS=28 (4 scan cycles, same n>=20 floor) │
│     and rule 3 fires on EITHER the pool or the window, naming   │
│     both. The window never REPLACES the pool — suppressing the  │
│     life figure would be #12 in reverse — it decides. Undated   │
│     records are excluded AND counted, never dated to "now";     │
│     absent window data degrades to the pooled rule and claims   │
│     no window. Same lesson as #15 on the other axis: a guard is │
│     only as good as the axes it was written on.                 │
│     docs/proposals/pf-window-verdict-2026-10-23.md               │
│     tests/test_improvement_pf_window.py                          │
│                                                                 │
│ 18. A GATE MUST NAME THE POPULATION IT PROTECTS.                 │
│     #14/#15/#17 fixed this for MEASUREMENTS; the event blackout  │
│     is the same defect in a GATE. `risk_state()` opened a 30h    │
│     stand-down on every High-impact USD release, but its only    │
│     caller is a crypto book (BTC..XAUT) — so "USD Congressional   │
│     Elections" (High, not in GOLD_MOVERS, which the module       │
│     already carries) stopped every crypto entry for 30h for a    │
│     reason the book does not act on. The trigger is now          │
│     is_gold_mover(). Same file: this-week + next-week feeds      │
│     overlap (3 releases written twice in one cache) — collapse   │
│     before the write; and the last-known-good seed merged        │
│     forever, so one blocked fetch (HTTP 429) replaced a 116-row  │
│     cache with 874 rows, 762 of them past. A fallback that only  │
│     grows is not a fallback: pruned to [-2d, +21d] on merge and  │
│     on read.                                                     │
│     docs/proposals/econ-calendar-blackout-scope-2026-11-01.md    │
│     tests/test_econ_calendar_blackout.py                         │
│                                                                 │
│ 19. A STATE FIELD IS A CLAIM SOME CODE MUST HOLD UP.             │
│     The 11-01 E9 repair left `released_unowned_position` in the  │
│     demo state: written by hand, not by any shipped code,        │
│     contradicted by the decision log (0 release rows), naming a  │
│     commit that does not exist, and never refreshed — so it      │
│     would still say "released 2026-11-01" after the next         │
│     release. Trading state was right; the receipt was a story.   │
│     Rule: every persisted field must be written by code, cleared │
│     on every transition that invalidates it, and false when a    │
│     test reads it. The append-only decision log is the audit     │
│     surface; the state file holds only what a strategy trades on.│
│     docs/proposals/release-receipt-2026-11-02.md                 │
│     tests/loop/test_release_receipt_and_reentry.py               │
│                                                                  │
│ 20. A PARAMETER IS A MEASUREMENT: READ IT AT ITS OWN SCOPE.      │
│     #9/#13/#15 fixed this for a NUMBER and #18 for a GATE. This  │
│     is the same rule for a KNOB. `data/exit-params.json` is      │
│     written by exit_calibrate.py from `live-trades.json` and     │
│     carries `"scope": "live-trades.json ONLY"` with a            │
│     `disaster_stop_pct` — a stop meant to be WIDE, because it    │
│     only bounds a tail (plateau runs 2.5-5%).                    │
│     `unified_trader._paper_open()` imported it and used it as    │
│     the paper INITIAL stop, which wants to be TIGHT, with RR     │
│     derived from it. So the paper book booked 2.50% stops and    │
│     +5.00% targets while the comment three lines above the call  │
│     cited the 2026-09-16 study's 2% / 4%. Every weekly           │
│     calibration moved the paper book too, silently: the OPEN     │
│     line printed prices and never the width or its source.       │
│     Found 2026-11-02 — all 5 open positions carried +5.00%.      │
│     Paper now owns `PAPER_INITIAL_STOP_PCT`/`PAPER_RR` and the   │
│     line states them. Two sub-lessons:                      │
│       (a) A SCOPE FIELD NO READER ENFORCES IS A COMMENT. The     │
│           write side emitted `scope` and `not_applicable_to`;    │
│           the one reader that mattered ignored both.             │
│       (b) SHARING A FILE IS NOT SHARING A MEASUREMENT. The 09-20 │
│           fix pointed "both tracks" at one file to stop them      │
│           drifting apart — right instinct, wrong unit. One       │
│           file, two meanings, one of them unstated.              │
│     docs/proposals/paper-stop-basis-2026-11-02.md                │
│     tests/test_paper_stop_basis.py                                │
│                                                                  │
│ 21. A GUARD MUST NOT BE ASKED TO JUDGE ITS OWN SUBJECT.          │
│     #14/#15 fixed the population a verdict is computed ON; this  │
│     is the population the CHECKER is handed. `_change_lines()`   │
│     asks `lead_is_coin_fragile(best_row, live_row)` — and        │
│     `best_row` is max(rows) over the SAME list the live config   │
│     is in, so "the incumbent is still best" (the normal, healthy │
│     outcome) makes challenger IS incumbent: lead 0.00pp, inside  │
│     its own band, erased by every drop, 0% of segments — all     │
│     three refusals fire on a row compared with itself. On the    │
│     2026-11-03 grid: "dropping DOGE cuts the challenger 26.79pp  │
│     -> lead -26.79pp" about sma_r=150 vs sma_r=150.              │
│     Two lessons:                                                 │
│       (a) A TIE IS RESOLVED BY LIST ORDER. `max()` returns the   │
│           first, LIVE is second in the grid, so an exact tie     │
│           always crowns the challenger and then refuses it for   │
│           a 0.00pp "lead". Say TIE; that is all the data says.   │
│       (b) A SELF-COMPARISON PASSES EVERY TEST THAT ASKS "is it   │
│           different?" — the guard needs an identity check, not a │
│           tighter threshold.                                     │
│     docs/proposals/incumbent-is-not-a-challenger-2026-11-03.md   │
│     tests/test_research_loop_coin_fragility.py                   │
│                                                                  │
│ 22. A BASELINE REBUILT UNDER AUTHORS' RULES IS STILL A BASELINE. │
│     The earlier lesson said "a simulation that cannot reproduce  │
│     its baseline has validated nothing" (#14/#15). This is that  │
│     rule applied to the research loop's own book. Re-deriving    │
│     `bt()` with two extra fields (close reason, worst adverse    │
│     excursion) reproduces all four folds to the cent             │
│     (49.07/6.66/26.47/42.05) — so the additions are free:        │
│       (a) THE STOP IS A FLOOR, NOT A LOSS DISTRIBUTION. All 118  │
│           stop exits book at −10.18% or worse (mean −14.28, min  │
│           −32.21); not one of 341 trades shows a −10% excursion  │
│           before booking. The loop sees day-start prices only,   │
│           so a −10% stop exits at the next close through the     │
│           gap. The median loss is −1.35% and never sees the      │
│           stop. Win rate 37.2% overall, 57.0% once the stop      │
│           exits are removed — the stop alone accounts for the    │
│           gap, so moving it moves the tail, not the win rate.    │
│           Same shape #20 found in the paper book, now on the     │
│           research book with a reproducing baseline.             │
│       (b) THE BAND AND THE ROW WERE FROM DIFFERENT UNIVERSES.     │
│           `run()` measures `per_coin` on 12 seeded draws over    │
│           22 non-BTC coins, then `lead_is_coin_fragile()` puts   │
│           the 24-coin median beside `loo_min/loo_max`. On        │
│           today's cache the 12 draws are 8 distinct universes    │
│           and both ends are repeated draws: floor 7.47 = DOGE    │
│           3x, ceiling 30.20 = LTC 2x. Full leave-one-out over    │
│           23 coins gives 7.47..45.64 (ceiling AAVE) and 34.26    │
│           is INSIDE it — the printed interval is a sampling      │
│           artefact in both tails, and it is what the "LIVE EDGE  │
│           DECAYED" sentence quotes.                              │
│           ✅ SHIPPED 2026-11-03 overnight: `coin_fragility()`    │
│           is now exhaustive by default (drop each non-BTC coin   │
│           once, `LOO_DRAWS=None`), stamps `basis` +              │
│           `distinct_universes` on every band, and the decay      │
│           sentence names the basis inline. Band is now           │
│           self-consistent, which exposes finding 2: the          │
│           incumbent sits INSIDE its own band on all 6 measured   │
│           rows, so `live_fragile` can essentially never fire.    │
│     docs/proposals/research-book-stop-and-universe-2026-11-03.md │
│     docs/proposals/loo-band-was-a-sample-2026-11-03.md           │
│     tests/test_research_stop_basis.py                            │
│     tests/test_research_loop_coin_fragility.py                   │
│                                                                  │
│ 23. A KNOB THAT IS NOT A PARAMETER IS STILL A KNOB.              │
│     #19 said the ranking was ranking the SIZER. This is the      │
│     same claim from below the config dict: `bt()` skips any      │
│     position under 5% of cash (`if sz < 0.05: continue`) and     │
│     no grid row varies it, so no consistency check INSIDE the    │
│     grid can see it. LIVE config, same folds, one literal:      │
│       floor 0.05    53.58  6.41 30.23 47.65  med 38.94  698 tr  │
│       floor <=0.03  54.44  7.48 29.95 43.32  med 36.64  709 tr  │
│       0.03/0.02/0.01/none are IDENTICAL — below 3% the knob is   │
│       dead, so the "parameter" is one step of a step function.   │
│     Per-fold floored-minus-none: −0.86 −1.07 +0.28 +4.33. The    │
│     median prefers the floor; three of four folds do not, and    │
│     fold 4 carries the whole 2.30pp. A tie-breaker, not a rule.  │
│     Before quoting a ranking, LIST WHAT THE RANKING FUNCTION     │
│     READS THAT THE GRID DOES NOT WRITE.                          │
│     docs/proposals/min-position-floor-2026-11-05.md              │
│     tests/test_research_loop_min_position_floor.py               │
│                                                                  │
│ 24. AN ADVICE CHANNEL THAT CANNOT CLOSE AN ITEM IS AN            │
│     UNBOUNDED COST. `_suggest()` ended with a hardcoded          │
│     "Explore: time-of-day filter…" literal that survived from    │
│     the first commit and printed on every run — every 40 min     │
│     — under the real findings. Nothing could retire it: the      │
│     volume-gate idea next to it only stopped repeating because   │
│     a human hand-edited the literal, which also erased the       │
│     record that it had been tried. Tested it 2026-11-05:         │
│     NEGATIVE — Asia t=-2.06 pooled flips to +0.15 in H2; NY's    │
│     +0.52 is one day (10-18, +3.21) and reads t=-0.12 without    │
│     it; London's rise is that same removal and flips its own     │
│     halves. No session effect is measurable at n=812.            │
│     Two lessons:                                                 │
│       (a) A HYPOTHESIS NEEDS A CLOSED STATE, NOT A DELETION.     │
│           Propose in `_explorations()`, close in                 │
│           `CLOSED_HYPOTHESES`; a closed key renders as an        │
│           `explore_result` carrying its verdict, so negative     │
│           knowledge survives and the task stops re-appearing.    │
│       (b) AN EARLY RETURN IS A SILENT POLICY ON EVERY SECTION    │
│           BELOW IT. `wait_for_data` returned at the n<20 gate    │
│           and dropped section 5 entirely — every closure record  │
│           vanished on a thin book. A thin book says nothing      │
│           about a hypothesis tested on a full one, so the        │
│           exploration list now runs on both paths.               │
│     docs/proposals/session-filter-closed-2026-11-05.md           │
│     tests/test_improvement_explore_closure.py                    │
│                                                                  │
│ 25. A LABEL THAT NAMES A BOOK IS NOT THE BOOK IT READS.          │
│     `expectancy_vs_fee.py` prints `trend_follower (REAL $)` and  │
│     reads `trend-state.json` -> `log`, a 53-symbol PAPER scan    │
│     log; real money lives in `real.closed`. Measured 2026-11-05: │
│       row printed   n=206  +1.451%/trade  t=+1.98              │
│       real.closed   n=18   +6.19% mean, median -1.03%, t=+0.97  │
│         pnl_usd null on 18/18, fee_status UNKNOWN 18/18         │
│         WITHOUT NEAR (one +109.8% record, entry BOOKED):        │
│         +0.09%, n=17, t=+0.05 — noise.                          │
│     `fee_audit.py` reads the same log and multiplies it by a    │
│     hardcoded `stake = 5.0`; the real records imply ~$5.49 and  │
│     paid $0.07 of BNB fee on ~$96 notional, so the 0.1594% RT   │
│     fee is ~2x the rate this book actually paid.                │
│     `check-books.py` §1 unit-checks four books and NOT          │
│     `trend-state.json`: paper gets a unit check, real money     │
│     does not. Three rules:                                      │
│       (a) WHEN A ROW NAMES A MONEY BASIS, GREP THE KEY IT       │
│           OPENS. `log` and `real.closed` are one keystroke      │
│           apart and a different universe.                       │
│       (b) A HARDCODED STAKE IS A FABRICATED SIZE. Read it from  │
│           the record or print UNKNOWN.                          │
│       (c) ONE RECORD THAT FLIPS THE SIGN IS NOT A TRACK         │
│           RECORD — print the remove-top-record figure beside    │
│           the headline. (Third instance: demo-0004-NEARUSDT on  │
│           the public ledger, the loop's stop tail, and this).    │
│     Readers NOT edited: owner-facing + one is a scheduled job   │
│     (09:50). Design ready for T0.10's own PR.                   │
│     docs/proposals/real-book-reported-on-paper-log-2026-11-05.md│
│                                                                  │
│ 26. A LEADER'S MEAN IS NOT A TRACK RECORD WHEN IT IS THE BOOK.   │
│     Rule 25(c) was written for a single record; this is the same │
│     defect at the level of a whole SYMBOL. `self_improve` printed│
│     "paper best symbol: NEAR/USDT n=504 net +9.33" every run.    │
│     The book that day: NEAR +5.55 net (after its own fees), the  │
│     other 13 symbols −10.57, book −5.02. "Best symbol" reads as  │
│     a property of the BOOK ("this is what we are good at"); the  │
│     measured fact is a CONCENTRATION — remove one name and the   │
│     sign inverts. The line now prints the leader's share of the  │
│     book's net, and when the book is non-positive it prints the  │
│     rest-sum instead of dividing by a denominator that would    │
│     make the share meaninglessly >100% (rule 15).                │
│     Corollary: a per-symbol card can only be read beside the     │
│     per-symbol population. 11 "losers" over n>=20 is not noise — │
│     it is the SHAPE of a single-name book, and the two lines now │
│     have to agree.                                               │
│     tests/test_best_symbol_states_risk.py                        │
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
