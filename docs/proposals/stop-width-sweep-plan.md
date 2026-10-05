# Stop-width sweep on the paper book — plan

**Written BEFORE running.** Method fixed here; deviations recorded with reasons.

## The question

The calibration file (`data/exit-params.json`) swept stop width on **30 real trades** and
picked 2.5%. A separate analysis of **2,337 paper trades** found tight stops (<1.5%) far
more profitable. They disagree. **This resolves it by running the calibration's own
method on the paper book's sample size.**

## Why the previous comparison was not decisive

`unified_trader.py` confounds stop width with entry path:

```
_paper_open            stop = _exit_params()["stop_pct"]  (2%)   or 0.02 fallback
_pullback_open         stop = 0.012                              (hardcoded)
_paper_loose_open      stop = 0.012                              (hardcoded)
```

**So "tight vs wide" was also "pullback/loose vs open".** Unusable as a stop test.

## Method — hold the strategy fixed

The paper book records, for every closed trade:

```
entry, tp, sl, size, pnl, reason, symbol, tf, ts_utc
best_px, best_ts_utc          (present on 68% — the peak reached during the trade)
```

**Reconstruct, for each trade, what it would have returned at a different stop width,
holding everything else fixed.** For stop width `w`:

```
initial_sl(w)   = entry * (1 - w)                      (long-only book)
did it hit?     = best_px <= initial_sl(w)             the peak-favourable path low
                          ^ see CAVEAT 1 - best_px is the BEST price, not the WORST
if hit:         exit at initial_sl(w), reason SL
else:           keep the recorded exit (TP / TIME / STALL), unchanged
```

**This is a re-simulation of the stop only.** The trade's other exits are taken as given,
because the paper book does not record the full tick path.

## Sweep values (fixed in advance)

```
0.008  0.010  0.012  0.015  0.020  0.025  0.030
```

**Reported for each: n, win rate, net, average per trade, exit-mix, and the count of
trades whose outcome changed.**

## CAVEATS — declared before results, not after

1. **`best_px` is the best price reached, not the worst.** A stop is hit by the *adverse*
   extreme, which this field does not give. **A trade whose `best_px` is above the stop
   may still have dipped through it.** Therefore this sweep **under-estimates stop-outs
   for wide stops and cannot faithfully replay tight ones.** *This is a real limitation,
   not a formality — see §Verdict rule.*
2. **68% coverage.** 32% of trades lack `best_px` and cannot be re-simulated. They are
   **excluded, and the excluded set is reported separately** so any bias is visible.
3. **Gap risk / path dependency ignored.** No intra-trade sequencing.
4. **Costs unchanged.** 0.28% round-trip assumed, as in the book.

## Verdict rule — declared now

- If the sweep shows a **monotone or plateaued optimum at 2.0-2.5%**, the calibration is
  corroborated and **my earlier tight-stop reading is an artifact of the strategy
  confound.** The stop-width proposal is **withdrawn.**
- If the sweep shows an **optimum below 1.5%**, the calibration's n=30 was too small and
  the proposal stands, pending the forward test.
- If the sweep is **flat or non-monotone**, neither is established and the honest output
  is "the paper book cannot answer this" — reported as such.

**Caveat 1 means a flat result is a likely outcome, and that is a legitimate finding.**
