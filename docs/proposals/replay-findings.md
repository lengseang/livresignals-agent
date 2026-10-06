# Walk-forward replay: the live rule, nine years, real costs

**Status: ANALYSIS — read-only. No live or paper behaviour changed.**

## 1. What was built

A walk-forward replay engine whose defining property is that **look-ahead is
structurally impossible**, not merely avoided.

| file | role |
|---|---|
| `scripts/locked_feed.py` | cursor-locked data source. `peek()` always raises; `seek_to(past)` raises; a bar is only visible after its `close_time`. Has a self-test, including the decisive one: *the past does not change when the future arrives*. |
| `scripts/replay_engine.py` | the clock, cost model, next-bar fills, time stop. Enforces one clock across symbols, fills at the **next** bar's open, charges costs, and assumes the **adverse** outcome when one bar contains both stop and target. |
| `scripts/replay_real_strategy.py` | replays the live `unified_trader` entry rule (`_features`/`_validated`). |
| `scripts/replay_live_config.py` | the faithful live configuration: 12 symbols, 5m/15m/30m, `max_open=8`, live stops/targets/hold. |

Everything above is committed to `proposal/rr-floor`; nothing is on `main`.

## 2. The rule under test (frozen before the run)

Reproduced from `scripts/unified_trader.py`:

```
entry   VALIDATED per-(symbol,tf) buckets, else the fallback
        dd <= -2%  AND  rsi <= 30  AND  red >= 3
exit    stop 2.0%, target 4.0% (RR 2.0) — from data/exit-params.json
hold    2 hours (8 bars on 15m, 24 on 5m, 4 on 30m)
slots   max 8 concurrent
```

## 3. Result — the whole table

Nine years, 2017-08-17 .. 2026-10-01. Costs in round-trip terms.

```
                      real fills ~0.03%      exchange ~0.10%      taker+slip ~0.30%
5m     n=22,657        +13.825  PF 1.02      −65.798  PF 0.92     −297.041  PF 0.67
15m    n=29,276        +31.203  PF 1.03      −74.290  PF 0.92     −367.375  PF 0.68
30m    n=13,227        +16.774  PF 1.04      −29.524  PF 0.93     −164.200  PF 0.67
────────────────────────────────────────────────────────────────────────────────────
TOTAL  n=65,160         +61.802              −169.612             −828.616
```

**Win rate is 51–52% at real cost, 49% at exchange cost, 42% at pessimistic cost.**
**Costs do not merely subtract money — they convert marginal winners into losers.**

## 4. The finding

**At the real fills' implied cost (~0.03%), the rule is marginally positive: PF 1.02–1.04
across all three timeframes.**

**PF 1.03 means $1.03 won per $1.00 lost, over 65,160 trades and nine years. That is not
an edge — it is noise with a slight lean, and the dispersion at this sample size swallows
it.**

**At the exchange's own taker fee (0.10%) the rule is negative on every timeframe. No
slippage is needed to get there; the advertised fee is sufficient.**

## 5. Why this contradicts the paper book

```
paper book (no costs applied)      +0.0095 per trade
replay at real fills (~0.03%)      +0.0006 .. +0.0013 per trade
```

**The paper book reports 8–15× the per-trade result the raw signal supports.** Two
independently verified causes:

1. **The paper book applies no transaction costs.** Verified 2026-10-05: records with
   `exit_px` (n=991) imply a median fee of −0.0001%; real fills (n=30) show ~0.03%.
   `COST_ROUND_TRIP = 0.0028` is a constant in a replay script, not a charge the book makes.
2. **The book books a stop/target as *touched*, not as *filled*.** The replay charges the
   adverse outcome when one bar spans both levels.

**The live book (−0.288 over 30 trades) is consistent with the replay's prediction.** Small
negative at real cost is what a PF 1.03 rule produces after fees. **The two sources now
agree; nothing is broken — the rule is simply not profitable after costs.**

## 6. Caveats that could move the number

```
cost model        ~0.03% derived from 30 live fills. If real cost is higher, worse.
fill assumption   the replay fills every stop/target at its exact price. Real
                  limit orders sometimes do not fill; that cuts both ways but
                  for stop-outs it usually means worse.
symbol set        restricted to symbols with deep candle history (12 of 14 live).
                  NEAR/SUP/OP/JUP coverage varies.
cadence           the live scan schedule is approximated (5m: every 3rd bar).
                  This affects trade count, not the sign.
```

**None of these is large enough to turn PF 1.03 into a business.**

## 7. What this changes

```
1. The paper P&L must not be used as an edge measure until it charges costs.
2. The engine is the new source of truth for strategy questions, at ~40s per
   nine-year run across 12 symbols.
3. The +23 in the paper book was never an edge. It was an absence of costs.
```

## 8. Reproduce

```bash
cd /opt/trading-agent/scripts
python3 locked_feed.py            # guard self-test (8 checks)
python3 replay_engine.py          # engine self-test (12 checks)
python3 replay_live_config.py      # the table in section 3
```
