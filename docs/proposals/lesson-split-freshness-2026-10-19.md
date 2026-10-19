# The lesson narrative quoted a measurement that expired in a day

**Date:** 2026-10-19
**Script:** `/home/ubuntu/hermes-home/scripts/trade_lesson_loop.py`
**Tests:** `tests/test_lesson_split_freshness.py` (8 new, RED 6 → GREEN 8)
**Repair:** 3,196 lessons re-derived, 2,267 changed, 0 unmatched, 0 verdict flips

**Snapshot:** `attachments/trade_lesson_loop.2026-10-19.py` — the verified loop
(the live copy lives outside this repo, at
`/home/ubuntu/hermes-home/scripts/trade_lesson_loop.py`; md5
`2624e5d0f323d50734ab182ec72f159a`). Copy it back to reproduce exactly.

## What was wrong

Commit `0c00a42` (2026-10-18) correctly retracted the "RR<2 loses — skip it"
advice: on DESIGNED geometry the RR<2 population is net-positive, so the retraction
is right. But the retraction's *evidence* was written into both producers as a
string literal:

    improve()      : "it is +9.95 in the first half and -1.20 in the second"
    write_rollup() : "it is +9.95 in the first half and -1.20 in the second"

Those numbers were a measurement of a 3,171-lesson ledger on 2026-10-18.
Re-computing the identical statistic on the 3,196-lesson ledger the next day:

| cut | RR<2 first half | RR<2 second half |
|---|---|---|
| 2026-10-18 (quoted) | +9.95 | **-1.20** |
| 2026-10-19 (measured) | +11.16 | **+0.06** |

The second half is now net-POSITIVE. The conclusion ("RR does not separate this
book") survives — the exact-2.0 band still does not clear the fee and the halves
are not a stable cut — but the sentence printed beside it was no longer true.
This is SYSTEM_MAP Rule 7 ("re-derive a threshold on the current book") applied
to prose: a number quoted at a level where the population has moved.

## Why it was not a one-line edit

The split point is fragile in a way that is invisible when you quote a number.
Most exact-2.0 fills arrive inside the same second (two timeframe lanes firing
together). Fifteen monthly cut points move the second-half net by about $18
because a cut can land *inside* one cluster and drop up to four near-identical
fills from the first half. A hardcoded pair therefore cannot be "kept up to
date" by hand — the definition of the split has to be fixed, or the number is
noise with a timestamp.

## Fix

1. `rr_split(lessons)` derives the split from the same ledger
   `threshold_validity()` already reads. It reports only when the result is
   identical under ledger order AND trade-time order, and returns `None` when
   the cut would fall inside one instant-bucket. No clean cut → no claim.
2. `rr_split_text(split)` renders the sentence, and it states the *shape*:
   - first half pays, second half loses → "shape is still present, but a
     half-split is not a stable edge";
   - anything else → "the first-half-pays / second-half-loses shape did NOT hold
     on this run, so the earlier decay claim is stale".
   A split whose halves agree can never imply a decay story.
3. `improve()` and `write_rollup()` build the sentence from `rr_split_text()`
   only. `improve(split=...)` uses a sentinel default so "caller passed None"
   (force the honest fallback) is distinguishable from "not passed" (read the
   ledger).
4. The rollup now carries `rr_split` as structured data — `first_net`,
   `second_net`, `cut_ts`, `order_independent` — so any later reader can see
   which split produced the sentence.

## Tests (mutation-checked)

- `test_the_hardcoded_halves_no_longer_describe_the_live_ledger` — parses the
  loop with `ast`, strips docstrings/comments, and fails if `9.95`/`first half`
  appears in any *printable* function body, or if either producer stops calling
  `rr_split_text()`. Reverting the rollup sentence to the constant re-fails it.
- `test_the_narrative_prints_the_derived_numbers_not_a_constant` — pins the
  printed numbers to `rr_split()` on the live ledger. Reverting `improve()`
  re-fails it (plus the fallback test).
- `test_split_refuses_a_ledger_it_cannot_cut_cleanly` — 300 fills at one instant
  must produce `None`. Removing the instant-bucket guard re-fails it.
- `test_split_is_order_independent`, `test_halves_that_agree_are_not_quoted_as_a_decay_story`,
  `test_the_live_sentence_states_what_the_live_halves_actually_do`,
  `test_missing_split_falls_back_without_inventing_a_number`,
  `test_the_repairable_ledger_prints_a_single_clean_rr_sentence`.

First draft of the source check scanned the whole file text and **missed** the
mutation, because the module docstring quotes the same sentence. The AST version
is what actually bites.

## Decision impact

No verdict changed (0 flips), no threshold moved, no trade was affected. What
changed is the sentence a reader sees: 2,267 stored lessons now print the
measured halves and the honest shape instead of a day-old pair. Old note:

    setup was below RR 2.0 — RR does not separate this book (the RR<2 population
    is net-positive overall), so this is not a reason to skip …

New note:

    setup was below RR 2.0 — RR does not separate this book: the RR<2.0 population
    is +11.16 in the first half (n=1071) and +0.06 in the second (n=1070) — the
    first-half-pays / second-half-loses shape did NOT hold on this run, so the
    earlier decay claim is stale. This is not a reason to skip the setup or to
    promote it

## Standing rule added

> If a lesson note quotes a statistic, that statistic is computed at write time
> from the ledger it describes — and when the statistic is fragile to the choice
> of cut point, the note must name the shape it claims, not just two numbers.

Follow-up worth doing separately: `threshold_validity().ok` is still `True`
with `coarse_signs_differ: False`. The field name promises "the two sides
differ" and the data says they do not; `ok` was redefined in 0c00a42 to mean
"RR_GOOD is the first paying band", so the two now disagree inside one dict.
Not changed here — it needs its own proposal because `ok` is consumed by the
rollup's "RR THRESHOLD STALE" alarm.
