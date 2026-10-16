# Learning: one named regime is not a contrast — the RANGING "stand down" was the book's own week

**Date:** 2026-10-16
**Cron class:** LEARNING
**Commit:** `improvement-loop: refuse a per-regime verdict on a single-valued regime field`

## What was found

`reports/improvement-report.json` (production, 2026-10-16T09:30Z) emitted a
priority-3 actionable suggestion:

```
regime_disable :: Regime RANGING at 40.8% over 142 trades
                 — consider standing down in this regime.
```

It looks like a regime signal. It is not.

The `regime` field first reaches the paper book on **2026-10-10** (the fix that
made the openers stamp `trade_judge`'s per-cycle regime). Measured 2026-10-16,
**every trade tagged since then carries exactly one value — `RANGING`**:

```
day         n   ranging   (unified-paper.json, post-multitf-collapse)
2026-10-10  61      18
2026-10-11  54      54
2026-10-12  29      29
2026-10-14  27      27
2026-10-15  14      14
----------------------------
            142 named   ·   2326 still "?"   (all pre-2026-10-10)
```

So the `RANGING` cohort **is the recent period**, and every trader before it is
`"?`". The two buckets are:

```
RANGING   n=142   40.8% WR   net -1.533   avg -0.0108
"?"       n=2326  47.5% WR   net -2.316   avg -0.0010
whole     n=2468  47.1% WR   net -3.848   avg -0.0016
```

The `RANGING` bucket's 40.8% is the book's **last-week win rate wearing a regime
label**. A conditioning variable observed at a single value cannot be contrasted
against anything, so nothing about the 40.8% is attributable to the *regime*: the
loss may be regime, period, or both, and this sample cannot separate them.

## Why this is the SAME defect the "?" guard already exists to prevent

`improvement_loop.py` carries a 2026-10-10 guard whose whole purpose is to stop a
per-regime verdict being derived from a bucket that holds the entire population:

> *"A 'Regime ? at 47.8% — stand down' line is not a regime signal; it just
> restates the book's own win rate with a question mark."*

That guard fires only on the all-`"?"` case (`unknown_regime and not
named_regime`). The moment the field started populating — with one value — the
guard stopped firing and the identical false actionable came back through a new
door: `RANGING` replaced `"?"` as the label on the same non-contrast.

**A guard written against a specific symptom (the `"?"` label) rather than the
underlying condition (a cohort that is the whole population) re-opens when the
symptom changes.** This is the sibling of the 2026-10-14 dedupe lesson: a key is
only as good as its discriminating power over the population it is applied to.

## The fix

A per-regime verdict now requires **at least two named regimes with n>=10 each**,
so the named cohort is a proper subset of the period rather than a synonym for
it. When exactly one named value is observed, the loop emits a priority-3
`regime_data_gap` note that prints the coverage numbers and states why no
stand-down is issued:

```
Regime attribution covers only ONE value ('RANGING', n=142 at 40.8%),
while 2326 trades carry no regime. A single-valued regime field cannot
isolate a regime effect — the cohort IS the period it was recorded in,
so its win rate is not attributable to the regime. NO per-regime
stand-down verdict is issued until >=2 regimes are observed with n>=10 each.
```

Same principle as the edge-freshness guard's *"a 1-trade window must not decide
anything"*: a cohort that is the whole population decides nothing.

## What this is NOT

- **Not** a claim that RANGING is harmless. It says the current sample cannot
  attribute anything to RANGING. If the field ever records a second value, the
  contrast becomes real and the verdict returns.
- **Not** a threshold change to the `<50%` rule; the rule is untouched. Only the
  *precondition* for applying it is added.
- **Not** a data-loss change: `by_regime` still reports both rows, so the owner
  sees RANGING n=142 / 40.8% — it is simply no longer dressed as actionable.

## Verification (2026-10-16)

```
A/B (git show swap, per the repo's method):
  baseline (HEAD improvement_loop.py) : test_single_named_regime... FAILED
                                        (the fake regime_disable fired)
  with-fix                            : 9 passed
production run : suggestion[regime] = regime_data_gap (was: regime_disable)  PASS
full suite     : 617 passed, 3 skipped   (was 615 — 2 net-new tests)         PASS
consumer check : trading_digest.py reads priority<=3 suggestions; the honest
                 note still ships, the false actionable no longer does        PASS
```

## Tests added (`tests/test_improvement_loop_freshness.py`)

- `test_single_named_regime_is_a_data_gap_not_a_stand_down` — the real-world
  case; fails on pre-change code.
- `test_one_named_regime_below_the_n_floor_does_not_warn` — a single named
  regime too thin to judge (n<10) must not warn.
- `test_named_low_regime_still_warns` — updated to supply a *second* named
  regime, since the old fixture (one named regime) encoded the false
  expectation. The rule keeps its teeth when the contrast exists.

## The generalisable lesson

**When a guard states a rule, ask what population the rule assumes — then check
the guard tests for that assumption, not for the label that happened to expose
it.** The `"?"`-guard and this fix are the same principle; only the trigger
differed. Before trusting a per-<dimension> verdict, assert the dimension has
**≥2 observed values with judgeable samples**; otherwise the "dimension" is a
period alias and its statistic is the overall statistic in disguise.
