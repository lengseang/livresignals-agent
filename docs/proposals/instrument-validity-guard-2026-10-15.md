# Instrument-validity guard in the lesson loop

**Status: APPLIED 2026-10-15 (overnight improvement shift). Loop script only; no
strategy, no order path, no live state touched. The paper book was not re-run.**

## What changed

`~/hermes-home/scripts/trade_lesson_loop.py`:

1. New `instrument_validity(closed)` — flags a book whose target was unreachable for
   the WHOLE population, using recorded fields only:
   - every trade has TP/SL geometry, AND
   - 0 trades exited at TP, AND
   - the target is IDENTICAL across the book (n >= 10) — i.e. the geometry is a
     constant, not a per-trade choice.

2. New `GEOMETRY-INVALID` verdict. When a book is flagged, EVERY lesson from it is
   written as `GEOMETRY-INVALID` (with the evidence in `verdict_note`) instead of a
   confident RR-based verdict.

3. Rollup gains `instrument_warnings`; the warning is pushed to `implications[0]`, and
   printed to stdout on every run.

4. `write_rollup()` extracted and called on BOTH paths — including the no-new-trades
   early return (previously the rollup was only written after new lessons, so the
   warning never appeared on a quiet hour).

## Why — the defect this closes

`docs/proposals/ensemble-geometry-defect.md` (2026-10-09) found that every ensemble
trade carried a target exactly +3.00% away while the best realised move was +1.24%:
**0 TP exits, unreachable by construction.** The loop still verdicted all 99 of those
trades as `AS DESIGNED` / `GOOD OUTCOME, POOR DECISION`, with RR notes — confident
lessons about a rule the instrument never tested. The skill stated the rule in prose
("if no trade ever reached its target, the TARGET was the problem") but nothing
enforced it.

## Verification (2026-10-15)

```text
ensemble (real)  : 99 closed, 0 TP exits, target [3.0] identical -> INVALID   PASS
unified  (real)  : 2965 closed, 382 TP exits, varied targets    -> valid     PASS
neg controls     : 1 TP exit -> valid | varied targets -> valid
                   n=5 identical -> valid (thin-data guard)      all False   PASS
forward path     : 12 synthetic TIME-exit trades -> 12 GEOMETRY-INVALID       PASS
idempotency      : production trade-lessons.jsonl byte-identical after a run  PASS
rollup on quiet  : instrument_warnings present after a no-new-trades run      PASS
```

## What this does NOT do

- Does **not** fix the ensemble runner. The geometry defect (`stop_pct = 0.02`
  hardcoded at `scripts/ensemble_paper.py:194`, and the `mids[0]` single-symbol bug at
  `:129-140`) is still open. This change makes the invalid instrument impossible to
  read silently — it does not repair it. Fixing the runner is a reviewed change with a
  replay, per the proposal.
- Does **not** alter the RR finding or any live selection.
- Does **not** rewrite the 99 existing ensemble lessons (that would rewrite history).
  The guard applies to NEW lessons; existing ones are surfaced via the rollup warning.
