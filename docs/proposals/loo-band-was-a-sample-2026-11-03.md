# The leave-one-coin-out band was a sample — and a self-consistent band cannot decay

**2026-11-03 · overnight improvement shift.** One script change
(`scripts/research_loop.py`), one test file extended. Read-only on every book,
every parameter, every state file. No promotion, no pause, no config change.
Nothing here touches an order path.

## Why this pass exists

The daytime shift (`research-book-stop-and-universe-2026-11-03.md`) measured
that the research loop's leave-one-coin-out band is a sampling artefact and
wrote it up **as a proposal, with the script untouched**. This pass ships the
repair and measures what the repaired instrument then says.

Nothing about the finding is new. What is new is that the fix was run.

## 1. What was wrong, restated with the shipped numbers

`coin_fragility()` drew **12 seeded universes** out of 23 coins and published
`loo_min`/`loo_max` over them. Measured on the live cache, those 12 draws are
**8 distinct universes**:

```text
DOGE x3   SOL x2   LTC x2   INJ ATOM ADA SUI ETH x1
```

The band is a min/max, so both tails were decided by which coin the shuffle
happened to land on:

```text
                floor        ceiling
published       7.47 DOGE    30.20 LTC     12 draws / 8 universes
exhaustive      7.47 DOGE    45.64 AAVE    23 coins, each dropped once
```

The floor was right by luck (DOGE is genuinely the binding coin). The ceiling
was **15.4pp too low** — the luckiest single name (AAVE) was never drawn.

Cost of exhausting it: the whole `research_loop.py` run is **4.3s** (23
universes x 4 folds vs 12 x 4). There was never a reason to sample a
population of 23.

## 2. What shipped

```text
LOO_DRAWS = None                       # None = exhaustive (new default)
coin_fragility(h, r, draws=None)       # draws=<int> still forces the old mode
  payload gains: basis, distinct_universes
lead_is_coin_fragile(...) gains: live_basis, live_draws
_fragility_line() / the DECAYED sentence now append [basis]
```

`basis` is not decoration. A band and a claim about that band are different
objects, and the reader could not tell `[+7.47,+30.20]` (8 of 23 universes)
from an exhaustive interval. Every band now states which it is, in the state
file, the findings markdown, and the sentence the owner receives.

**Pinned by 4 new tests**, all of which fail against `HEAD~` and pass against
the fix (mutation-checked, not assumed):

```text
test_loo_is_exhaustive_by_default_and_covers_every_coin_once
test_sampled_mode_still_reports_how_many_distinct_universes_it_saw
test_the_live_band_contains_the_published_floor_and_an_honest_ceiling
test_decay_sentence_names_the_basis_so_a_sample_cannot_look_like_population
```

Baseline A/B: `4 failed, 19 passed` on the pre-change file; `23 passed,
1 skipped` on the fix; file restored and verified byte-identical.

## 3. 🔴 The finding the fix EXPOSED: a self-consistent band cannot decay

This is the part that matters more than the repair, and it was not visible
until the band and the median came from the same operation.

With `median` (full universe) and `loo_min`/`loo_max` (drop one coin) both
computed by `coin_fragility()`, the incumbent sits **inside its own band on all
six measured rows**:

```text
cfg (topn/hold/risk)   median   exhaustive band      inside?
3/21/0.01  (LIVE)       34.26    [ 7.47, 45.64]      YES
3/21/0.02               25.73    [ 2.62, 39.73]      YES
5/21/0.01               24.65    [18.03, 31.24]      YES
3/14/0.01               18.65    [17.47, 40.17]      YES
3/30/0.01               16.10    [ 5.70, 27.53]      YES
2/21/0.01                8.44    [ 4.76, 18.35]      YES
```

The decay test asks *"is the incumbent worse than a leave-one-out of itself"*.
Removing one coin out of 23 is a small perturbation, so the band is necessarily
wide around the median — and `live_fragile` can essentially never fire. Before
the fix it fired (wrongly) because the sampled ceiling was too low; **the
sampling bug was the only thing making the decay branch reachable at all.**

⇒ Neither state is a working detector. The interval is the wrong instrument for
the question. `live_fragile` is currently a sentence that can be computed but
cannot be triggered by real decay — the same shape as the ensemble guard that
labelled a defect for 8 days without deciding anything.

**Not fixed in this pass**, deliberately: replacing the test needs a decision
about what "decayed" means (a drawdown against the config's own record? a
rolling out-of-sample refit? a threshold on the drop distribution?), and
choosing that after seeing the data is exactly the pre-declared-validation
error this repo has made four times. Recorded here so the next research task
starts from the mechanism, not from a threshold.

## 4. One thing that is NOT a finding

The typical drop-one median is **below** the full-universe median on 4 of 6
rows and above it on 2 (`3/14`: +12.50pp, 22 of 23 drops improve it;
`2/21`: +1.42pp, 17 of 23). That split is a property of a median over 4 folds
under a 6-config grid — not evidence about any coin, and not a stable rule.
**Do not read the +12.50pp row as "this config is fragile"**: it is the
weakest-ranked measured row, and it sits one rank above `2/21`, which shows the
same sign.

I looked for a load-bearing rule here — "top-N selectors invert LOO", "worse
configs invert LOO" — and **neither holds**. The two inverted rows are the
2nd-weakest (`3/14`) and the weakest (`2/21`), so rank does not explain it; the
crown (`1/23`) and `2/21` (`17/23`) are the two extremes of the same `topn=3`
family, so the selector does not either; `topn=4` is untested. There is no rule
to ship. Stated so it is not re-derived.

## 5. What is NOT claimed

- **No claim about which config is best.** The crown did not move
  (`sma_r=150`, 34.26%, 4/4) and no message was emitted.
- **No claim that the live config has decayed or not.** The repaired instrument
  cannot answer it (§3).
- **No claim about the strategy.** The research book's stop and universe
  findings from the daytime pass stand unchanged and are not re-litigated here.
- **The band is still a min/max over 23 universes, not a confidence interval.**
  Exhaustive removes the sampling artefact; it does not make min/max a
  quantile. A quantile report is a further change and is not shipped.
- **`loo_drop_max` semantics unchanged** — still `median - min(medians)`, still
  the worst single-coin drop. With an exhaustive band that number is now the
  true worst drop rather than the worst *sampled* drop, so it can only grow.

## 6. Verification

```text
venv/bin/python scripts/research_loop.py
  -> best median 34.26% (4/4) | live median 34.26% | no change (silence by design)
  -> band [+7.47,+45.64] [exhaustive leave-one-coin-out over 23 coins]

venv/bin/python -m pytest tests/test_research_loop_coin_fragility.py -q
  -> 23 passed, 1 skipped

mutation check (pre-change file swapped in, then restored, diff empty)
  -> 4 failed, 19 passed   (the 4 new tests, and only those)
```
