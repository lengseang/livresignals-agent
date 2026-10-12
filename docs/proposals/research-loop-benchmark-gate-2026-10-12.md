# Learning: a benchmark you compute but never compare against is decoration

**Date:** 2026-10-12
**Cron class:** LEARNING
**Commit:** `research_loop: gate NEW BEST on beating BTC buy&hold (rule 3)`

## What was found

`scripts/research_loop.py` is the nightly edge-hunt that decides whether to
announce a better regime-adaptive config. Each run it computed the two
benchmarks and **printed them in the findings header**:

```
benchmarks: BTC median -21.45%, basket median -50.93%
```

Then it ranked the candidate configs with:

```python
def score(r):                      # pre-fix
    return (r["pos_folds"], r["median"])   # configs vs EACH OTHER
```

`bench_btc_median` was never an input to `score()`, and never appeared in the
change-detection block. So the announcement path was:

```python
if score(best) > score(pb):
    changed.append(f"NEW BEST config: ...")   # unconditional
```

**The defect:** a config that is positive in every fold *but returns less than
BTC buy&hold* would be crowned "NEW BEST" and pushed to Telegram. That is
precisely the claim HARD RULE 3 (SYSTEM_MAP.md §4) forbids: *"ALWAYS compare
to BTC buy&hold. If you don't beat it, you have no edge."*

It is the same class of failure as the 2026-10-12 cosmetic-disable bug: a
quantity the system *knows* (benchmark) that no decision *reads*. The report
looked rigorous — the number was right there on line 2 — while the verdict
ignored it.

## The fix

One predicate, `admissible(row, bench_btc_median)`, and one gate in `main()`:

- a "NEW BEST" is only announced when `config median >= BTC buy&hold median`;
- the verdict is now written into `reports/research-findings.md` as an explicit
  line (BEATS/LAGS by N pp), so the referee is visible, not implicit;
- **fail-safe**: a missing/None benchmark BLOCKS the claim rather than passing
  it. A loop that cannot prove it beat buy&hold must not claim an edge.

The live "LIVE EDGE DECAYED" warning is unchanged — decay is a factual
observation, not an edge claim.

## Why it matters

The current window (a bear tape: BTC -21.45%) still passes honestly —
`+41.23pp`. The bug was latent: invisible while BTC loses, and the moment a
bull tape flips BTC above the config, the loop would have announced an "edge"
that the system's own rule says does not exist. Latent logic bugs are the
expensive kind — they fire exactly when the regime changes and you are least
watching.

## The generalisable lesson

**A benchmark is only a benchmark if a decision depends on it.** Computing and
displaying a reference number is theatre; wiring it into the yes/no is the
work. When you add a comparison quantity, grep for the *decision* it is meant
to change — if nothing branches on it, it is decoration.

## Verification

- `tests/test_research_loop_benchmark.py` — 5 tests pinning the predicate and
  the end-to-end suppression in `main()`.
- **Mutation-checked**: bypassing the gate (`ok_bench = True`) makes
  `test_new_best_suppressed_when_lagging_benchmark` FAIL; restoring it passes.
- Full suite: **235 passed** (baseline 230 + 5 new), 0 failures.
- End-to-end on real data: `scripts/research_loop.py` ran clean; the findings
  file now carries `BENCHMARK (rule 3): BEATS BTC buy&hold by +41.23pp —
  ADMISSIBLE`.
