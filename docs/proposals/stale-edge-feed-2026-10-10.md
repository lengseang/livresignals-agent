# The improvement loop was reading a frozen edge feed (and a phantom regime)

**Date:** 2026-10-10 · **Status:** fixed · **Class:** measurement integrity

## What was wrong

`improvement_loop.py` calls its per-edge breakdown "the accuracy-improvement
core": it promotes edges with WR>=58% + positive EV toward real money and flags
edges below 45% for tightening or stand-down. The promote/demote verdicts are
only as good as the trades they are computed from.

Checked against the actual book on 2026-10-10:

```
unified-paper.json closed trades:        2778
trades carrying an `edge` tag:            362   (13.0%)
first edge-tagged trade:        2026-09-04
LAST  edge-tagged trade:        2026-09-13   <- 26 days before "now"
book's most recent trade:       2026-10-10
```

The edge feed stopped on **2026-09-13**. Every "Edge 'supertrend' DECAYING"
line in the report since then was computed on a ~4-week-stale 362-trade subset,
presented without any marker that it was not current.

## Why it stopped

Four places open paper trades; only two ever stamped an `edge`:

```
unified_trader.py   opener                        edge tag
  ~228  _paper_open          (validated native)   NONE      <- ACTIVE
  ~386  _pullback_open       (pullback)           "pullback" <- DEAD (returns False since 2026-09-06)
  ~486  _paper_loose_open    (loose native)       NONE      <- ACTIVE
  ~540  _imported_strategy_open (imported)        edge var   <- stopped firing ~2026-09-13
```

The two ACTIVE native openers wrote no `edge` field, so 87% of trades fell back
to the `loose`/`validated` heuristics in the loop and the genuine tags came only
from imported strategies, which stopped firing on 2026-09-13.

## The second defect: a phantom regime

`by_regime` reported exactly one bucket, `"?"`, holding all 2778 trades — no
paper trade carries a `regime` field (0/2778). The loop turned that into:

> "Regime ? at 47.8% over 2778 trades — consider standing down in this regime."

which is not a regime signal at all: it restates the book's own win rate with a
question mark. It cannot be acted on. (The judge computes a real regime every
cycle, but the paper openers do not persist it.)

## The fix

1. `_paper_open` stamps `"edge": "validated"`; `_paper_loose_open` stamps
   `"edge": "loose"`. New trades are attributed from now on.
2. `improvement_loop._edge_freshness()` measures tagged share + age of the last
   tag; the report now carries `edge_meta` and raises a priority-1
   `stale_edge_data` suggestion whenever the feed is >2 days stale or <50%
   tagged, so a frozen feed is LOUD, never a silent stale verdict.
3. The regime section emits `regime_data_gap` (honest data-gap note) when the
   only bucket is `"?"`, and only warns `regime_disable` on a REAL named regime.
4. `tests/test_improvement_loop_freshness.py` pins all four behaviours.

## The lesson

A metric that silently freezes is worse than a metric that is absent: the loop
kept printing confident per-edge verdicts on a dead feed for ~4 weeks. The
guard is the same shape as the window-coverage guard (2026-10-10): before
trusting a derived number, assert that its INPUT is present and freshly fed —
not merely non-empty.
