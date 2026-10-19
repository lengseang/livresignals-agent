# The lesson loop crashed hourly for 24h and quiet mode hid it

**Date:** 2026-10-19
**Script:** `/home/ubuntu/hermes-home/scripts/trade_lesson_loop.py`
**Tests:** `tests/test_lesson_main_writes.py` (5 new; RED 5 against the buggy
file → GREEN 5 against the fix; the 24 pre-existing lesson tests stay green)
**Snapshot:** `attachments/trade_lesson_loop.2026-10-19b.py`, md5
`089fb59de0422a2a7dbab88f28b716a3`. The previous snapshot
(`trade_lesson_loop.2026-10-19.py`) is the buggy build — diff them.
**Repair:** 26 unlearned trades written, 0 verdict flips, 0 duplicate ids

## What was wrong

Commit `07869cc` (2026-10-19, "stop printing a measurement that expired in a
day") replaced the hardcoded RR<2 half-split with a derived one. It wired the
derived value into `repair_lessons()` (line 785) and `write_rollup()`
(line 724) — but **not** into `main()`'s write loop, which kept the reference
to a bare `split` that nothing in `main()` defines:

    "what_would_improve": improve(t, verdict, pop, split=split),

`improve()` has a sentinel default (`split=_SENTINEL` → read the ledger on
disk), so the *only* reason the call site passes `split=split` at all is the
variable, and the variable does not exist. Every hourly run with at least one
new resolved trade died with:

    new from unified  : 20
    new from ensemble : 6
    could not write lessons: name 'split' is not defined

## Why nobody saw it for 24 hours

Three independent properties of the loop combined:

1. **The NameError is raised inside `with open(LESSONS, "a")`**, on the first
   trade. The append handle is already open, so the file is created if absent
   and the run exits `1` with nothing appended.
2. **`--quiet` treats "no stdout" as "nothing to report".** The cron is
   `trade-lesson-loop`, `0 * * * *`, launched with `--quiet`; the error goes to
   stdout but the quiet path only forwards the *success* summary, so the failure
   produced no Telegram message at all. A crash and a quiet hour are
   indistinguishable to the reader.
3. **The existing test suite never called `main()`.** `test_lesson_trailed_sl`,
   `test_lesson_split_freshness`, `test_lesson_rr_boundary` all test
   `improve()` / `rr_split()` / `repair_lessons()` directly. The derived split
   was verified perfectly; the function that has to pass it was not executed by
   any test. 8/8 green, production dead.

⇒ **A guard that tests the unit is not a guard that tests the wiring.** The
introduced defect lived exactly in the gap between the two functions each test
exercised.

## Fix

1. `split = _current_split()` at the top of `main()`, next to `seen` — one
   derivation per run, from the ledger the run is about to append to, memoised
   by `(size, mtime)`. Comment states the failure so it cannot be "cleaned up"
   back into the loop.
2. `_drop_torn_tail(handle)` plus a `flush()` before it: the ledger is truncated
   to its last **parseable** complete line. This closes the neighbouring hole —
   a kill *during* a multi-trade append leaves a torn JSON line, and `_seen()`
   swallows the parse error per line but the torn row is then unlearnable and
   every subsequent run re-counts it as new forever. Handles both shapes: no
   trailing newline, and newline present but body torn.
3. The write-failure message now names the count written and the exception
   class, so a partial run is legible: `could not write lessons after 1:
   RuntimeError: simulated crash`.

## Tests (mutation-checked)

- `test_main_writes_every_new_trade` — the exact regression. Calls `main()` with
  one trade in each book and asserts rc 0, two rows, both books present, and
  that `what_would_improve` is non-empty.
- `test_main_is_idempotent_on_a_second_run` — byte-identical file after a quiet
  run.
- `test_a_failure_mid_write_leaves_a_resumable_ledger` — `improve()` raises on
  the 2nd trade; the file must contain complete parseable lines only.
- `test_drop_torn_tail_repairs_both_torn_shapes`, and a no-op check on a
  healthy ledger.

Verified by file swap (per `git-state-verification.md` §8): buggy file →
`5 failed`; fixed file → `29 passed` across all four lesson test modules.

## Measured effect

```
before : 3,196 lessons · unified 3,064 · ensemble 126
after  : 3,222 lessons · unified 3,090 · ensemble 132
         +26 (20 unified, 6 ensemble), 0 torn, 3,222 unique ids
run 2  : "nothing new — loop is up to date", md5 unchanged  (idempotent)
--quiet: silent, rc 0
```

The 20 unified trades that were waiting are **all `ALIGNED`** (8 TP, 6 STALL,
4 TIME, 2 SL) — i.e. the day the loop was blind was a day of well-shaped trades,
not a loss cluster. The 6 ensemble trades are `GEOMETRY-INVALID`, as every
ensemble trade now is.

## Decision impact

No verdict changed, no threshold moved, no trade affected. What changed: 26
trades that had no lesson now have one, and a failure mode that silently
suppressed the loop for a day now fails loudly with a count.

## ⚠️ Not fixed here — the quiet path is still blind to failure

`--quiet` suppresses the error text as well as the success summary. The cron
will therefore still not message the owner when the loop crashes; it will only
be visible to whoever reads the script's output. **That is the more important
defect and it needs its own decision** (what should the cron say when a job it
runs returns rc≠0?), because the same shape applies to every other `--quiet`
job on this box. Recorded, not changed — changing what the scheduler posts is
an owner-facing behaviour change, not a bug fix.
