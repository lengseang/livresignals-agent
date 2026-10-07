# The ensemble forward book was outside the lesson loop

**Status: FIX SHIPPED 2026-10-07 (read-only learning change; touches no strategy,
no order path, no live parameter).**

## The gap

`strategy-generation-loop` states, in the section *"The loop closes through the
lesson loop"*:

> **It wraps everything here**, so the generated strategies' paper trades feed
> per-trade lessons automatically with no extra wiring: `ensemble-paper.json`
> closes → `trade_lesson_loop.py` reads them on its next hourly pass.

**That was false.** `trade_lesson_loop.py` defined:

```python
STATE = AGENT / "data/unified-paper.json"
```

and read **only that book**. `data/ensemble-paper.json` was never opened.

Measured: **0 of 28** closed ensemble trades had a lesson in
`data/trade-lessons.jsonl`. The forward record of every generated strategy was
being collected and then never learned from — the exact opposite of the owner's
*"every attempt is a lesson."*

## Why it matters — the forward record it was hiding

The ensemble book's first 28 closes (`data/ensemble-paper.json`):

```
closes      28          opens_total 30
win rate    8/28 = 28.6%
net         -0.5733  USDT   (each 5 USDT flat notional)
exit mix    TIME 27 · SL 1
RR          every trade at 1.5  (below the population's 2.0 losing threshold)
by ensemble ens|momentum|5m   n=18  win 5  net -0.3561
            ens|momentum|30m  n=10  win 3  net -0.2172
```

**27 of 28 trades exited on `TIME`** — neither target nor stop reached before the
hold expired. That is structurally the same failure shape documented in
`trade-lesson-loop` §2 (the wide-stop mechanism: the exit machinery never fires,
so the trade times out). The backtest gate reported these momentum ensembles at
**100% coherence** across 5m and 30m; the forward book does not support that.

## The fix

`trade_lesson_loop.py` now iterates an explicit list:

```python
PAPER_BOOKS = [
    ("unified",  AGENT / "data/unified-paper.json"),
    ("ensemble", AGENT / "data/ensemble-paper.json"),
]
```

Design constraints preserved:

- **Never pooled.** Each trade is classified against **its own book's**
  population. The rollup is `verdicts_by_book` / `population_by_book`.
- **Every lesson tagged** with a `book` field. Legacy lessons (no field) default
  to `"unified"` in the rollup, so historical counts do not move.
- Still **cannot trade**, still read-only on every state file except its own two
  outputs.
- **Idempotent** and `--quiet`-silent when nothing new (verified: second run
  prints nothing).

## What the loop now says about the ensemble book

```
[ensemble] AS DESIGNED                      20
[ensemble] GOOD OUTCOME, POOR DECISION       8
```

All 8 wins are filed **GOOD OUTCOME, POOR DECISION** — they came from a setup
whose population loses (RR 1.5), so the loop's own rule applies: *the win is not
evidence the setup works and must not count toward promoting it.* The 20 losses
are **AS DESIGNED** — not surprising, not a lesson, no stop to loosen.

⇒ **None of the ensemble book's forward record counts toward promotion.** That is
the correct read, and it is now the loop's read rather than a manual observation.

## Verification performed

1. Ran the patched loop against a **copy** of the data first (`/tmp/lltest`),
   confirming output shape before touching production.
2. Snapshotted production (`trade-lessons.jsonl` and `.json`) to `/tmp` before
   the run.
3. Ran once on production: 2554 → 2582 lessons (28 added, all `book=ensemble`).
4. Re-ran with `--quiet`: **silent** (idempotent).

## The generalisable lesson

**A wiring claim in documentation is a claim, not evidence.** When a document
says *"X feeds Y automatically"*, open Y and read which files it opens — the
constant is the evidence. This is the same class as every other correction in
this repo: the number/claim carried forward and never recomputed from source.
