# The 5% minimum position is the only knob nobody chose — and it is inside the LIVE config's own win rate

**2026-11-05 · overnight autonomous improvement pass.** Read-only on every
book, parameter, order path and state file. No sizing change, no promotion, no
pause, no config change. One commit adds a test file, one proposal and one
journal entry. The research loop was **not** run through its entrypoint (the
proposal it would regenerate is the subject of §2). Suite run this session:
`868 passed, 5 skipped, 1 xfailed, 1 failed` — the single failure is the
known one-bar cache pin, §4.

## 1. The line, and why nobody is looking at it

`research/06-walkforward.py::bt()` rejects any candidate position smaller than
5% of cash:

```python
sz = min(cash * risk / (v / px[c][i]), cash / max(1, topn - len(pos)))
if sz < 0.05:
    continue
```

Every row of the nine-config grid in `scripts/research_loop.py` runs through
this line, and the LIVE config the owner is shown beside them does too. The
grid varies `topn / hold / risk / sma_r`. **No row varies the floor**, and
`run()` records neither how often it fires nor how far the book moves when it
does not.

Yesterday's pass (2026-11-04) proved the `risk` term never binds, so the floor
and the `topn` cash cap are the only two things actually sizing the book.

## 2. What the floor is worth, measured on the LIVE config

Same cache, same folds, same entries, one literal changed:

```text
floor  fold returns                    median  trades
0.05   53.58  6.41  30.23  47.65       38.94     698
0.03   54.44  7.48  29.95  43.32       36.64     709
0.02   (identical to 0.03)             36.64     709
0.01   (identical to 0.03)             36.64     709
none   (identical to 0.03)             36.64     709
```

Three readings, all of which matter:

- **The floor costs 2.30pp of median while being right on exactly one fold.**
  Per fold, floored minus no-floor: `−0.86, −1.07, +0.28, +4.33`. Removing the
  floor improves f1, f2 and f4 and loses only f3 (+0.28). The sign flips per
  fold, and the whole 2.30pp of median is carried by f4's +4.33. It is a
  **tie-breaker**, not a systematic rule.
- **The median is the only statistic that prefers the floor.** Three of four
  folds do not. A single fold deciding a median is the same shape as #11, where
  one bar of cache moved the crown.
- **Below 3% the knob is dead.** 0.03, 0.02, 0.01 and none are *identical*:
  once the floor is under ~3% no candidate survives to be skipped at all. The
  5% is not a smooth risk parameter — it is a step function with exactly one
  live step, and that step is what the nightly ranking sits on.

Consequence for the published output: **the nightly "NEW BEST" and the
LIVE-vs-BTC headline are conditional on a constant that is not in any config
dict and is not reported.** Yesterday's finding (the ranking is ranking the
sizer) is confirmed from a second direction; this time the offending knob is
not even a parameter.

## 3. What was shipped

`tests/test_research_loop_min_position_floor.py`, three tests:

1. **the floor is live** — trade counts at 0.05 and at no-floor differ, and a
   `MUTANT_NO_FLOOR` fixture (literal intact, skip neutralised) reproduces the
   no-floor book exactly, so the assertion bites on behaviour, not on text.
2. **the rank is decided by the floor** — the per-fold differences change sign.
   Pins the *shape*, never the level, because the cache advances one bar a day
   and every level-pinned test in this repo has gone red on a refresh (#11).
3. **the floor shift is not a verdict** — the median shift must stay inside one
   fold's spread, so if a future cache makes the floor genuinely dominant the
   test fails rather than letting this write-up become an understatement.

Mutation evidence (each run this session):

```text
baseline                                  3 passed
literal moved to 0.5 (structural guard)   3 errors  ("floor literal appears 0x")
skip neutralised via test fixture         caught by test 1's MUTANT assertion
"is live" test pointed at MUTANT instead
  of 0.05 (behavioural guard)             1 failed, 2 passed
```

## 4. Red left standing, named

`tests/test_research_stop_basis.py::test_the_traced_book_is_the_book_the_loop_prints`
is `1 failed` this session. It is the same daily-cache level pin already
recorded three times (2026-11-03 x2, 2026-11-04). Not touched: it needs a shape
assertion, and this pass's test file is the model for how.

## 5. The paper book, measured in passing (no change made)

The journal's standing rule is that any quoted gross must say `collapsed`.
Re-measured this session with the production collapser:

```text
3571 raw -> 2796 collapsed, 775 removed
gross  raw +24.669   collapsed +16.155   phantom +8.514
removed by reason: SL 298, TIME 292, TP 107, STALL 78
```

**The 775 phantom rows are 34.5% of the raw gross, i.e. +8.51 of +24.67.** The
collapsed mean per trade is +0.578% on a $5 notional = 2.9c, against an assumed
fee of 0.144% per side (1.4c). The book's edge and its fee assumption are the
same order of magnitude; that is not new, but it is the number to hold next to
any "+$24" quoted from the raw file.

## 6. What is NOT claimed

- **Not that 5% is wrong and should go.** Removing it makes the median look
  worse and two folds better; picking after reading this is the
  pre-declared-validation error this journal has recorded four times.
- **Not a new best config.** No row was re-ranked, nothing was promoted.
- **Not that the LIVE config has decayed.** The band is degenerate (§2 of
  yesterday's proposal) and this pass did not touch that question.
- **Not that the paper book's edge is real.** §5 is a unit check, not an
  expectancy claim.

## 7. The rule

> A knob that is not a parameter is still a knob. The grid can vary every key
> in the config dict and still be ranked by a literal three lines below it —
> and because it never appears in any row, no consistency check inside the grid
> can see it. Before quoting a ranking, list what the ranking function reads
> that the grid does not write.

And the corollary from §2: when a knob below 3% changes nothing, the grid is
not sampling a parameter, it is sampling one step of a step function.
