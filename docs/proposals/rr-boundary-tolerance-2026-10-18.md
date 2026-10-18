# Learning: the RR boundary split one 2:1 design into two opposite verdicts — and the correction showed the threshold itself is stale

**Date:** 2026-10-18
**Cron class:** LEARNING
**Files:** `~/hermes-home/scripts/trade_lesson_loop.py`,
`tests/test_lesson_rr_boundary.py` (new)
**Scope:** lesson/verdict layer only. No order path, no parameter, no strategy,
no paper book touched. `data/trade-lessons.jsonl` byte-identical after the run
(md5 `8fdc62c4…` before and after).

---

## 1. What was found

`classify()` compared the raw float RR to `RR_GOOD = 2.0`:

```python
good_setup = rr >= RR_GOOD
```

The dominant geometry on this system is an exact 2:1 — stop 1x, target 2x — and
`abs(tp-entry)/abs(entry-sl)` lands a hair either side of the literal:

```
entry=1.3607    tp=1.3879    sl=1.3471    -> 1.9999999999999836
entry=2421.71   tp=2470.1442 sl=2397.4929 -> 2.0000000000000187
```

The **same designed ratio**, differing only in which way the division rounded,
received four different verdicts:

```
119  GOOD DECISION, POOR OUTCOME  "RR 2.00 is inside the winning population"
 56  AS DESIGNED                  "RR 2.00 below 2.0; this population loses"
 28  ALIGNED
 21  GOOD OUTCOME, POOR DECISION  "RR 2.00 is inside the losing population"
```

Three of those sentences print the identical string `RR 2.00` and contradict
each other. The 56 `AS DESIGNED` rows are the damaging ones: a trade designed at
the threshold was labelled a member of "the measured population that loses", and
`improve()` then advised *"skipping it would have avoided this trade entirely"* —
a stand-down recommendation for the exact geometry the owner chose.

A second, independent defect rode along: the note formatted `f"RR {rr:.2f}"`,
which **rounds 1.996 up to `2.00`**, so a genuinely sub-threshold trade also
emitted the self-contradicting line `RR 2.00 below 2.0`.

Measured on the real ledger (3162 lessons, 2325 RR-classified):

```
verdicts that change : 87
  63  AS DESIGNED                 -> GOOD DECISION, POOR OUTCOME
  24  GOOD OUTCOME, POOR DECISION -> ALIGNED
contradictory "RR 2.00 below 2.0" notes before : 63
```

## 2. The fix

Two pieces, both pinned by `tests/test_lesson_rr_boundary.py` (RED: 4 failed /
2 passed before; GREEN: 9 passed after):

1. **`_rr_meets_good(rr)`** — one tolerance-aware comparator (`RR_TOL = 1e-6`,
   ~5 orders of magnitude above double noise on these price magnitudes, and far
   below the next real design at 2.5). `classify`, `improve` and `why` each had
   their own raw comparison; they now share one, so a boundary trade can no
   longer get a verdict, an advice line and a "why" line that disagree.
2. **`_rr_text(rr)`** — renders the RR so the printed number agrees with the
   verdict it sits inside. A boundary trade prints `RR 2.00`; a trade that would
   round *into* the threshold prints three decimals (`RR 1.996`).

Mutation-tested: restoring `rr >= RR_GOOD` re-fails exactly the three boundary
tests; removing the 3dp branch re-fails exactly the rounding test.

## 3. The finding behind the finding

With the boundary honest, the 2:1 cohort became readable for the first time —
and it is not what the constant says it is.

```
exact 2:1 cohort   n=241   WR 22.8%   net -8.73   (TP 9, SL 76, TIME 150)
```

`RR_GOOD = 2.0` was derived by `win_loss_separation.py` / `rr_circularity_check.py`
when "trades at RR>=2 carried all the net" was true of the book. Re-derived
band-by-band on today's ledger:

```
rr band      n     net      WR
< 2.0     1300   -48.48   24.8%
2.00-2.05  228   -10.53   21%     <- the threshold band itself LOSES
2.05-2.10   12    -1.06    8%
2.10-2.15   11    -0.17   36%
2.40-2.45   35    +3.19   89%
2.45-2.50  198   +21.65   94%
2.50-2.55  184   +21.26   98%
```

The first net-paying band at n>=20 is **2.40**, not 2.0. The `>=2.0` side nets
+55.69 *only* because of trades above 2.4; the 2.0–2.1 gap is net-negative and is
currently being described to the owner as "the winning population… the loss is
variance, not design". The TP-exit rate tells the same story: 3.2% at the 2.0
boundary versus 42.4% in the 2.1–2.5 band — at 2:1 with this hold horizon the
target is being placed where the instrument does not reach it, which is the same
"unreachable target" shape as the ensemble-book finding, one band lower.

**The threshold was not moved.** Where the population changes sign is a strategy
decision, not a reporting one, and this loop does not edit parameters. What was
added is `threshold_validity()`: every rollup now reports the net on each side of
the boundary, the first net-paying band, and — when the constant is not that
band — an explicit `RR THRESHOLD STALE` implication naming the gap (0.4). A
coarse "do the two sides differ in sign?" test was written first and **rejected**:
it returned `True` on this book, because the high band carries the aggregate.
The check that ships asks the question the constant actually claims: *is the
threshold the first paying band?*

## 4. Verification

```
RED first        : 4 failed, 2 passed
GREEN            : 9 passed
mutation 1       : rr >= RR_GOOD restored -> 3 failed (exactly the boundary tests)
mutation 2       : 3dp branch removed     -> 1 failed (exactly the rounding test)
full suite       : 647 passed, 3 skipped  (638 baseline + 9 new)
production run   : trade-lessons.jsonl md5 UNCHANGED — idempotent
rollup           : threshold_validity ok=False, first_paying_band=2.4,
                   coarse_signs_differ=True (which is why coarse was not enough)
```

## 5. Lesson

**A verdict line is a sentence, and every part of it must be true at once.** The
verdict said `AS DESIGNED`, the note said `RR 2.00`, and the clause said `below
2.0`. Each fragment was defensible in isolation; the sentence was false. The
check is not "is each number correct?" but "can a reader put these words
together and be misled?" — and here 63 trades were told they were the losing
population while printing the threshold value.

Second: **fixing a boundary is the cheapest way to discover the threshold is
wrong.** The float bug was worth 87 mislabelled verdicts. Correcting it exposed
~240 more that were labelled correctly *by the code* and wrongly *by the
constant* — the 2.0–2.1 band, net-negative, sitting inside "the winning
population". A boundary you cannot see across is a boundary you cannot audit.
When the label finally agrees with itself, re-run the derivation that set the
threshold; it may have been measured on a book that no longer exists.

Third, a caution for the next pass: the coarse version of `threshold_validity`
passed. Aggregate sign on each side of a threshold can be correct while the
threshold band is the worst cohort in the book — the high band's profit hides the
boundary's loss. Any check on a threshold must be local to the threshold.
