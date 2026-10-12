# Learning: an autonomous disable is only as real as its weakest reader

**Date:** 2026-10-12
**Cron class:** LEARNING
**Commit:** `unified_trader: enforce EDGE disables on the native openers`

## What was found

The self-improve engine (`scripts/self_improve.py`) walks proven-losing edges
and writes them to `data/signals-state.json['disabled']`:

```
disabled: [pullback, supertrend, ema-cross, bollinger, momentum, breakout, loose]
```

`_imported_strategy_open` honours that list. **The three NATIVE paper
openers did not** — `_paper_open` (validated), `_paper_loose_open` (loose) and
`_pullback_open` (pullback) never read it.

Evidence the disable was cosmetic for native edges:

- the engine wrote `APPLIED: disabled edge 'loose'` at 2026-10-11 02:40
- **44** loose trades still *closed* after that timestamp
- **4** loose positions were still *open* at audit time
- a pre-fix live scan opened loose positions on essentially every cycle

So the exact defect the imported-track comment claims to have fixed
("disabled strategies actually stop opening (not cosmetic)") was still live
for every native edge.

## The fix

One reader — `_edge_disabled(edge)` — consulted by all three native openers
(`validated`, `loose`, `pullback`). Missing/unreadable state file returns
`False`, so a broken state file never blocks trading.

## Why it matters

A learning loop that writes a decision nobody enforces is worse than no loop:
the report says the edge is off, the book keeps trading it, and the ledger
quietly disagrees with the control state. The failure is invisible precisely
because both halves look healthy in isolation.

## The generalisable lesson

**Every autonomous control must be read by the thing it claims to control.**
When adding a state-file gate, the test is not "did I write the flag" but "how
many openers can still bypass it". Grep the writers *and* every reader.

## Verification

- `tests/test_edge_disable_enforced.py` — pins the reader and each gate;
  mutation-checked (removing the loose gate fails the test).
- Full suite: 230 passed, 0 new failures. The 341 `tests/loop/` errors are
  pre-existing on HEAD (baseline 224 passed) and unrelated.
- End-to-end: a live `unified_trader.py` scan after the fix opened
  **0** loose positions while loose signals were present in the market.
