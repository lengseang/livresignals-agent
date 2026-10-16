# Instrument-validity warning made actionable: report the realised-move distribution

**Status: APPLIED 2026-10-16 (overnight improvement shift). Loop script only —
`~/hermes-home/scripts/trade_lesson_loop.py`. No strategy, no order path, no live
state touched. The paper books were not re-run; the lesson ledger is byte-identical.**

## What changed

`instrument_validity()` (added 2026-10-15) flagged a book whose target was
unreachable for the whole population and emitted a `GEOMETRY-INVALID` verdict.
That guard stated a *binary* fact — "0 TP exits, target unreachable" — which is
true but not actionable: it cannot distinguish a target 0.2% too far from one 40x
too far, and it does not say what geometry *would* have been reachable.

This change adds the **realised-move distribution** to the same evidence dict,
from recorded fields only (`entry` → `exit`, percent):

- `realised_move_pct`: `{n, min, median, p90, max}` — what the instrument
  actually did. The `max` is the ceiling a reachable target must clear.
- `max_best_move_pct`: the furthest intra-trade excursion (`best_px`), when
  recorded — shows whether the target was ever even *touched*.
- `target_exceeds_max_move`: True when the (identical) target is above the best
  move the book ever produced — the mechanical proof of unreachability.

The rollup implication and the stdout warning now **print these numbers**, so the
line reads as a diagnosis with an instruction ("fix the TARGET or the HOLD
HORIZON — the realised max is the ceiling") rather than a bare verdict. The
previous bare sentence remains the fallback when a book records no `entry`/`exit`,
so nothing regresses on books that don't carry those fields.

## Why — the defect this closes

Measured on `data/ensemble-paper.json` (2026-10-16):

```
closed            101
exit mix          TIME 95 · SL 6 · TP 0
target            identical +3.00%  (every trade rr=1.5 at a hardcoded 2% stop)
realised move     min -2.00% / median -0.048% / p90 +0.837% / max +2.269%
```

The **maximum realised move over 101 trades is +2.27%**. The target is +3.00%.
It was never reachable — not "rarely", not "unluckily", *never*. The old warning
said "0 TP exits", which a reader could mistake for bad luck. The new warning
says the best the instrument ever produced was +2.27%, which is unambiguous: the
+3% target clears the observed ceiling by construction and the 48-bar hold
expires long before such a move. The binding constraint is the **exit horizon or
the target**, not the stop (6/101 hit the stop; 95/101 timed out).

This is the same failure shape as the two prior geometry findings on this system
(RSI-2 +5% target, setup RR 1.5) — a hardcoded stop deriving an identical,
unreachable TP. The fix is a reviewed change to `ensemble_paper.py` (still open:
`stop_pct = 0.02` hardcoded at `:194`, `mids[0]` single-symbol at `:129-140`).
This change does not repair the runner; it makes the diagnosis specific enough to
act on.

## Verification (2026-10-16)

```
ensemble (real)   : 101 closed, 0 TP exits, target [3.0]% -> INVALID
                    realised max +2.269% < target 3.0% -> target_exceeds_max_move True   PASS
edge: no exit fld : INVALID, realised_move_pct None, no target_exceeds key, no crash     PASS
edge: varied tp   : valid                                                                 PASS
edge: one TP exit : valid                                                                 PASS
edge: thin n=5    : valid (thin-data guard)                                               PASS
edge: empty book  : valid, move dist None, no crash                                       PASS
production run    : trade-lessons.jsonl md5 UNCHANGED (599f86b1...) — idempotent           PASS
rollup (production): implication[0] now carries the realised-move distribution             PASS
--quiet           : silent (nothing new)                                                    PASS
full suite        : 615 passed, 3 skipped                                                  PASS
```

## What this does NOT do

- Does **not** fix `ensemble_paper.py`. The geometry defect is still open; this
  only makes its evidence actionable.
- Does **not** rewrite the 79 existing `AS DESIGNED` / 20 `GOOD OUTCOME, POOR
  DECISION` ensemble lessons (that would rewrite history). The guard applies to
  NEW lessons; existing ones are surfaced via the rollup warning.
- Does **not** change any selection, sizing, or live parameter. Read-only on
  every state file except its own two outputs.
