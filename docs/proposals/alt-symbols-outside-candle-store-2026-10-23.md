# 43.7% of the paper book trades symbols the candle store has never seen

Overnight shift, 2026-10-23. Found while checking whether the book's newest
cohort could be replayed from candles. It could not — for the largest single
group of symbols in it.

## Finding

`data/trading.db` holds **10 symbols**:

```text
ADA ARB AVAX BNB BTC DOGE ETH LINK SOL XRP
```

The unified paper book has closed trades on **14**:

```text
NEAR  445   <-- no candles
JUP   337   <-- no candles
OP    315   <-- no candles
SUI   289   <-- no candles
```

```text
closed trades on symbols WITH  candles : 1789
closed trades on symbols with NO candles: 1386   (43.7% of 3,175)
```

Verified the absence is real, not a naming variant: `LIKE '%NEAR%'`,
`'%JUP%'`, `'%OP/%'`, `'%SUI%'` all return zero rows, and
`count(DISTINCT symbol)` is 10.

## Why this is not a running defect in the scanner

`unified_trader.py` computes entries from **live** `ex.fetch_ohlcv` and closes on
`ex.fetch_ticker` — not from `trading.db`. So live scanning is unaffected, and
the 13:16 AVAX/LINK trades today were correctly priced even though their
`trading.db` series had frozen at 03:30.

The damage is in every consumer that reads the DB to **replay, calibrate or
learn**:

```text
paper_learner.py       reads 8 series — all core symbols, UNAFFECTED
ensemble_paper.py      reads the DB for margin_pct / reachability
weekly_regime_refit.py reads the DB
hunt*, replay_*, winrate_* — all read the DB
```

A symbol missing from the DB is not "unmeasured" to these scripts; it is
invisible, and the book's largest cohort simply drops out of any replay. The
`has-candle` vs `no-candle` split also differs in outcome
(+0.01190/trade vs +0.00101/trade, n=168/190 in the post-cost-fix cohort), so
the omission is not neutral with respect to the population being scored.

## The second half: 7 of the 10 ingested symbols are stale

```text
BTC ETH SOL   5m last 2026-10-23 17:50   (current)
ADA ARB AVAX BNB DOGE LINK XRP
              5m last 2026-10-23 03:30   (14.5h old, all five timeframes)
```

`ingest_all.py` prints `ingested 2145 candles across 15 series / OK — all series
fresh` every 15 minutes and returns 0. It is telling the truth about a
3-symbol universe: `config.SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]`, and
`3 symbols x 5 timeframes = 15 series`. There is **no alt ingester in any
crontab**, so the 7 alts were bulk-loaded once and silently froze.

`data_harness.py` reports `candle store (sqlite) OK 0.1h 10 symbols` — the store
MAX is fresh because BTC is fresh. This is the `check_data_health()`
aggregate-blind-spot documented in `leng-trading-stack`, one level up: the
health check is per-**store** when the question is per-**series**.

ADA has 43 of an expected ~288 5m bars today.

## What is NOT claimed

- Not that any live entry used stale data — the scanner does not read the DB.
- Not that the alts' absence caused a bad measurement that was acted on. The one
  scheduled learner (`paper_learner.py`) reads core symbols only.
- Not that the freeze began today. 03:30 is where the series stops; when the
  bulk load happened was not established.

## Proposed fixes (none applied — no live-path change)

1. Make `ingest_all.py` source its symbol list from the traded universe, or add
   the 4 missing symbols to `config.SYMBOLS`. The 15-series count should be
   asserted against the symbol list, not printed as evidence of health.
2. Add a **per-series freshness gate** to `data_harness.py`: fail when ANY
   series is older than k bars, not when the store's MAX is old.
3. Record, per trade, whether its symbol was covered by the candle store at
   entry. A book that cannot say which of its rows are replayable will keep
   scoring itself on a subset it cannot name.
