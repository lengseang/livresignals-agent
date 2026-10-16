# Learning: the strategy lab's 7 "survivors" were boundary artefacts — the split-half gate tested one line, not an edge

**Date:** 2026-10-16
**Cron class:** LEARNING
**Commit:** `strategy-lab: gate on boundary robustness, not one split point`

## What was found

`strategy_lab.py` (Hermes-side, `/home/ubuntu/hermes-home/scripts/`) has been
running every 15 minutes for ~1183 runs. Its gate requires a candidate to be
positive in **both halves** of the last 4000 candles — a legitimate rule 2, the
one the `strategy-generation-loop` skill calls "the one that matters".

It has published **7 candidates**, all SOL/USDT 1h, all breaching a prior channel
high with a 2% stop:

```text
cfg (entry,exit,rr)   n     WR      net     h1       h2
(24,24,1.5)          124   50.8%  +0.3228  +0.0372  +0.2584
(24,24,2.0)          112   43.8%  +0.3860  +0.0168  +0.3696
(24,48,1.5)          152   50.0%  +0.3340  +0.0864  +0.2076
(24,48,2.0)          145   43.5%  +0.4740  +0.1776  +0.3732
(48,24,1.5)           85   48.2%  +0.1120  +0.0204  +0.1144
(48,48,1.5)          105   46.7%  +0.0560  +0.0424  +0.0820
(48,48,2.0)          101   41.6%  +0.2170  +0.0336  +0.2520
```

Every one clears rule 2. But look at the two halves: **h1 is tiny (0.017–0.178)
and h2 is 2–20x larger.** The "edge" lives almost entirely in the second half.

## The falsification test, run

The split point is `len(rows)//2` of a **rolling 4000-candle window**. It is one
arbitrary line. Move it by ±400 candles (~17 days on 1h) and re-measure:

```text
cfg            h1(now)  h2(now) | split+400 h2 | split-400 h2
(24,24,1.5)    +0.0372  +0.2584 |   -0.0496    |   +0.0828
(24,24,2.0)    +0.0168  +0.3696 |   -0.0972    |   +0.0624
(24,48,1.5)    +0.0864  +0.2076 |   +0.0444    |   +0.1548
(24,48,2.0)    +0.1776  +0.3732 |   +0.0012    |   +0.2460
(48,24,1.5)    +0.0204  +0.1144 |   -0.0936    |   +0.0432
(48,48,1.5)    +0.0424  +0.0820 |   -0.0900    |   +0.0880
(48,48,2.0)    +0.0336  +0.2520 |   -0.0888    |   +0.0792
```

**5 of 7 flip negative in the second half when the boundary moves 17 days.**
The verdict depended on where the line was drawn, not on an edge. This is the
exact failure the skill's `references/testing-gate-functions.md` names:
*"Boundary fragility is a FINDING, not a flaky test"* — and the lab was not
checking for it.

## Why this was invisible

1. **The gate tested one boundary and stopped.** A binary "both halves > 0" test
   at a single split cannot distinguish an edge from a line-drawing artefact.
2. **The candidate record reported `first_half_net: +0.0372` as a PASS**, with no
   threshold on magnitude. $0.0372 over ~62 trades is ~$0.0006/trade — noise that
   merely happened to be on the right side of zero.
3. **The `published` counter was a rewrite count.** The same 7 names are rewritten
   every 15-minute run (`d.mkdir(exist_ok=True)` + overwrite), so after 1183 runs
   `state.json` read `published: 2882` — presenting 7 boundary artefacts as 2882
   distinct survivors.

## The fix

Added **rule 5 — boundary robustness** to `strategy_lab.py`:

- New `split_validate(rows, entry_lb, exit_lb, rr, shifts)` re-measures the
  strategy at each boundary in `SPLIT_SHIFTS = (0, -400, +400, -800, +800)`.
- The gate now requires `worst_half_net > 0` across **all** boundaries. A
  candidate that is fragile at any tested split is rejected and recorded with the
  offence (`failed boundary-robustness: worst half -0.0972 ... fragile at
  [2400, 2800]`).
- The published record carries `worst_half_net`, `splits_tested`,
  `boundary_robust`, and the skill body prints the worst-half figure.
- `state.json` now tracks `survivor_names` (unique) and `published_writes`
  (rewrites) separately; `published` is the unique-survivor count.

## The result of the fix, measured

Re-running the fixed gate over the same 54 combinations:

```text
combinations evaluated : 54
passed the gate        :  0
rejected               : 54      (47 failed split, 7 failed boundary-robustness)
```

**Zero survivors.** The 7 published candidates are all boundary artefacts; the
lab has no genuine survivor. This is a *truer* zero than the previous seven — it
is the gate working, not a regression. Per the skill: a low pass rate is the gate
working, not a bug.

## What this does and does not change

**Does not** touch any live path. `strategy_lab.py` is observational: it scans
`data/trading.db` read-only and writes only to `data/strategy-lab/` and skill
files. Nothing it produces is wired to an order.

**Does** mean the 7 `strat-sol-*` candidate skills should not be read as
survivors. They remain on disk as the lab's output, now labelled by the fixed
gate as artefacts; the next lab run will overwrite them (the fix gates before
publish, so no new ones will appear until a candidate is genuinely robust).

## Durable rule

**A split-half gate with a single split point is not a test of an edge; it is a
test of one line.** Always re-measure at shifted boundaries and require
robustness across all of them. Report the worst half, not the midpoint half. A
candidate whose net/gross ratio is a few percent of the sample size is at the
mercy of the boundary — quote the dollar figure and the failure to survive a
±17-day shift alongside it.
