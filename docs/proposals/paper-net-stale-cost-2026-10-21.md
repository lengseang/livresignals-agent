# A persisted net is a measurement, not a formula

**Date:** 2026-10-21
**Loop:** improvement (Loop 2 — the measure layer)
**Status:** fixed, tested
**Files:** `scripts/paper_net.py`, `tests/test_paper_net_stale_cost.py`, `SYSTEM_MAP.md` #13

---

## What was wrong

`paper_net.net_of` is the reader every autonomous consumer routes through:

    improvement_loop  →  self_improve  →  night_tuner

Its first branch was:

```python
pnl_net = record.get("pnl_net")
if isinstance(pnl_net, (int, float)):
    return float(pnl_net), True      # "the writer already charged it"
```

The comment was true. The value was not NET.

`pnl_net` is a **number frozen at write time**, not a formula. A trade closed
while `unified_trader` still held the old `PAPER_COST_RT = 0.0003` keeps 0.03%
of position value as its cost forever. The 2026-10-11 fix corrected the
writer's constant to the measured `0.15452%` and updated the reader's *fallback*
to match — but every record already on disk was blessed by this branch and
never re-derived.

## The measurement

Live `data/unified-paper.json`, 2026-10-21:

```
records carrying pnl_net          514
  charged $0.0015  (0.03% of $5)  226   <- stale constant
  charged ~$0.0077 (0.155%)       288   <- measured rate
true round trip                   0.15452%  ($0.007726 on $5, 5.2x higher)
```

One ledger, three different cost rates in the `cost` column.

```
reported net (before)   +$3.04
honest net  (after)     +$1.63
overstatement            46%
```

The error was concentrated in exactly the trades the loop is most likely to
promote: any edge whose gross edge sits between 0.03% and 0.155% per trade
(the fee band) read as *profitable* if its records were old.

## Why it stayed invisible

Before 2026-10-11 the reader's fallback and the writer's constant were the
**same** stale number, so "trust the recorded value" and "re-derive from size"
produced identical answers. The fix changed one side. A stale constant
outlived the fix that replaced it, wearing the authority of a recorded field.

This is the same failure family as SYSTEM_MAP #9 ("never quote a measurement;
re-derive it at write time") — except the stale number was persisted in the
ledger instead of written into a comment, so no reader could see it was old.

## The fix

A recorded `cost` is believed only when it is consistent with the **live**
measured rate:

| record shape | treatment |
|---|---|
| `cost >= 50%` of `size * live_rt` | trusted exactly (authoritative writer) |
| `cost > 0` but far below | **re-costed** `gross - size * live_rt` |
| `cost == 0.0` and `pnl_net == pnl` | trusted — deliberate `PAPER_CHARGE_COST=0` book |
| no `cost`, no `size` | gross, flagged `costed=False` — never zero |

Two deliberate asymmetries:

1. **Never re-cost downward.** A record charged *above* today's rate (an older,
   more expensive measured fee) is left alone. Fixing a stale number must not
   move the book in the flattering direction.
2. **The reader does not overrule `PAPER_CHARGE_COST=0`.** That path writes
   `cost = 0.0` *and* `pnl_net == pnl` — distinguishable from a stale charge
   (which has `pnl_net != pnl`). An operator switch at the writer is not a
   measurement to be corrected.

Both were caught by the new tests, the second only after the first test run
went red — the `test_zero_cost_...` case is why the guard is `cost > 0 and
cost < threshold` rather than `cost < threshold`.

## Verification

```
sweep of all 3105 records vs an independent reference implementation:
  0 mismatches
book net       +$3.04 -> +$1.63   (gross unchanged, as designed)
full suite     705 passed, 3 skipped
downstream     improvement_loop still reports validated +1.51 / loose +3.63
```

## Rejected as NOT a defect (same day, same ledger)

Investigated and confirmed honest — recording negatives so they are not
re-raised:

- **TP overshoot.** 14 TP exits recorded >6% gross against a ~5.03% target
  looks like a fill above the ask. Re-checked on 15m bars: 5/5 verifiable
  cases had the bar high at or above the booked TP, so the "overshoot" is the
  exit printing at the sampled price after the target was already through,
  not a phantom fill. 9 cases are unverifiable because OP/NEAR/JUP/SUI are not
  in the candle store — that coverage gap is a DATA issue, not a P&L one.
- **`sl == entry` records (833, 27%).** These are the documented breakeven lock
  (`unified_trader` sets `sl = entry` at +0.5%), not a missing stop. The lock
  fires at a +0.5% CLOSE, so a wick that reverses through entry legitimately
  exits at ~breakeven. Booked P&L matches `exit_px` to 0.001% — no defect.

## Follow-up worth doing

`unified-paper.json` still stores the three-rate `cost` column. The reader now
neutralises it, but a one-off re-cost pass writing `cost` and `pnl_net` at the
current rate would make the stored ledger self-consistent and let the reader
go back to a plain trust. Not done here: rewriting 3105 closed records is a
ledger mutation that should be an explicit, announced migration.
