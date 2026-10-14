# Learning: a disable loop that can disable EVERYTHING is a kill switch

**Date:** 2026-10-14
**Cron class:** LEARNING
**Commit:** `self-improve: never disable the last open edge (the paper book froze)`

## What was found

On 2026-10-12 the fix `unified_trader: enforce EDGE disables on the native
openers` made the self-improve disable list REAL: after it, every opener —
`validated`, `loose`, `pullback` AND all imported edges — routes through
`_edge_disabled()`. A disable finally did what it said.

The very next nightly runs then did what the rule says, all the way:

```
logs/self-improve.log:
  APPLIED: disabled edge 'loose'      (n=17 wr=29% net=-0.29)
  APPLIED: disabled edge 'validated'  (n=34 wr=38% net=-0.09)
```

`data/signals-state.json` ended with **all eight edges disabled**:

```
disabled: [pullback, supertrend, ema-cross, bollinger, momentum,
           breakout, loose, validated]
```

With every opener gated, the paper book **stopped opening entirely**.

## Evidence the book was dead (not merely quiet)

- `data/unified-paper.json` `closed` froze at **2026-10-12T18:16**; the book has
  run to 2026-10-14 with **zero** new closed trades and **zero** open positions.
- the live scan kept firing signals into the void — `logs/unified.log` shows
  `scan done: paper signals fired=14`, `=8`, `=3` … **with no `PAPER OPEN`
  lines**. The last `PAPER OPEN` in the log predates the disable.
- the improvement loop reported `stale_days: 1.6, stale: false` (threshold 2.0)
  — it *just* passed the freshness gate, and its per-edge "DECAYING" verdicts
  were computed on a book that had stopped moving.

A frozen book still looks like a healthy one: the file exists, it parses, the
metrics are self-consistent. They are just the metrics of a book that is no
longer trading.

## Why the rule emptied the set

The disable predicate is individually reasonable — **n>=15, WR<42%, net<0** on
the NET basis fixed 2026-10-13. That is exactly the trap: after the measured
~0.16% round trip, *every* edge on this book is a net loser. Judged edge by
edge the rule is right; applied to the whole set it is a kill switch. There was
no floor — nothing said "but not if it leaves you with nothing to trade".

## The fix

In `self_improve.py` step 3, before applying any disable, compute the set of
edges that would remain ENABLED. If applying the rule would leave **zero**,
keep the strongest candidate (highest net, then n), refuse the rest, and report
the refusal LOUDLY (`REFUSED to disable the last open edge '…'`). A second,
belt-and-braces check surfaces an already-frozen book as a warning on the card
(`WARNING: EVERY edge is disabled …`) rather than in a log nobody reads.

The predicate is extracted to `_should_disable()` so the guard and the disable
loop read ONE definition and cannot drift.

## Why it matters

This is the sequel to `edge-disable-enforcement-2026-10-12.md`. That fix made
a disable real; this one covers the case the fix created — a disable that is
real *and applied to everything* is not a risk control, it is a kill switch.
An autonomous component needs a FLOOR, not just a threshold.

## The generalisable lesson

**Before an autonomous loop can remove its last resource, it must be unable to
remove its last resource.** Thresholds say when to act; floors say when acting
would be self-destructive. Any "disable/deactivate/stop" loop over a set the
loop itself defines needs a cardinality floor.

## Verification

- `tests/test_self_improve_last_edge.py` — pins the predicate, the guard logic,
  the non-tripping case, and the wiring in `main()` (5 tests).
- Full suite: **601 passed, 3 skipped**, 0 new failures.
- Live prove: with `validated` + `loose` the only enabled edges, a real run of
  `self_improve.py` **refused** to disable `validated`, disabled `loose`, and
  left exactly one open edge. A following `unified_trader.py` scan opened
  **7 paper positions** (all carrying the `regime` tag) — the learning track is
  live again after ~1.5 days frozen.
