# Paper trades carried no regime — per-regime analysis was disabled

**Date:** 2026-10-10 · **Status:** fixed · **Class:** measurement integrity

## What was wrong

`improvement_loop.py` reports a `by_regime` section so the loop can spot a
regime where the book decays and stand down there. It could not: **0 of 2778
closed paper trades carried a `regime` field**, so `by_regime` collapsed to a
single `"?"` bucket holding the whole book. The loop turned that into:

> "Regime ? at 47.8% over 2778 trades — consider standing down in this regime."

which is not a regime signal — it restates the book's own win rate with a
question mark. The loop's own guard (added earlier today) downgraded it to an
honest `regime_data_gap`, but the *underlying* gap was never fixed.

## Why it happened

The Judge computes a real regime every cycle (`verdict["regime"]["regime"]`,
printed in the `JUDGE:` line), but the four paper openers wrote the trade record
without it:

```
unified_trader.py   opener                        regime stamped?
  _paper_open          (validated native)          NO  <- ACTIVE
  _pullback_open       (pullback)                  NO  (dead: _is_pullback == False)
  _paper_loose_open    (loose native)              NO  <- ACTIVE
  _imported_strategy_open (imported)               NO  <- ACTIVE
```

The regime was computed one line above the openers and simply discarded.

## The fix

Thread the Judge's regime into each opener as an optional parameter and stamp
it on the record:

- `_paper_open(sig, regime=None)`, `_paper_loose_open(sig, regime=None)`,
  `_pullback_open(sig, regime=None)`, `_imported_strategy_open(sig, regime=None)`
- each appends `"regime": regime or "?"` to the trade dict
- the scan loop passes the cycle's regime: `regime = (verdict.get("regime") or
  {}).get("regime", "?")`, set once after the Judge and used at all four sites
- defaulted to `None` so no existing caller or test breaks; a missing regime
  stays the visible `"?"` sentinel, never a fabricated one

Once trades carry a regime, `by_regime` splits into real named buckets and the
loop's `regime_disable` path (a REAL regime below 50% over n>=10) becomes live.

## Tests

`tests/test_regime_attribution.py` (4 tests):
- `_paper_open` stamps the supplied regime
- `_paper_loose_open` stamps the supplied regime
- no regime supplied → `"?"` (visibly missing, not guessed)
- openers still work with the defaulted argument (no caller breakage)

`tests/test_improvement_loop_freshness.py` continues to pass (5 tests) — the
`regime_data_gap` guard still fires while the bucket is all `?`, and
`regime_disable` still fires on a real named regime.

## Scope / risk

**Paper-only, measurement integrity.** No order path, no live parameter, no
position touched. The change is additive to the *record shape* of new paper
trades; existing closed records keep their shape (they simply have no `regime`,
exactly as before). This is the same class and shape as the edge-tag fix
committed earlier the same day (06b6dd2).

## The lesson

Same lesson as the stale-edge-feed fix, one step further: a value that is
**computed every cycle and then discarded at the write site** is invisible until
you open the writer. The Judge was faithfully producing a regime; the analysis
downstream was starved because nobody persisted it. Before concluding "the book
cannot answer this", check whether the answer is being computed and dropped —
then persisting it is a one-line fix, not an analysis.
