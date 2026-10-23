# The multi-timeframe fix could only run where the multi-timeframe bug wasn't

**Date:** 2026-10-23
**Status:** implemented (paper_book)
**Tests:** `tests/test_paper_book_multitf.py` (18, of which 5 new)

## What was believed

`SYSTEM_MAP.md` rule #6 and the 2026-10-15 proposal both say the multi-timeframe
double-count is fixed: `collapse_multitf()` removes "~500 phantom rows (17%)"
before any statistic is measured. On 2026-10-22 a *second* clause was added
because the ledger still held ~520 of them — the exact-fingerprint key requires
identical `entry` **and** identical `pnl`, and bookings a tick apart fail both.

That clause is the subject of this note. It did not work either.

## Defect — the clause was unreachable in the regime the bug lives in

The new clause was written as a FALLBACK:

```python
gap = abs((db - da).total_seconds())
if gap > window_s:            # <-- only past the exact-fingerprint window
    ... cross-timeframe test ...
if a.get("entry") != b.get("entry"):
    return False
if a.get("pnl") != b.get("pnl"):
    return False
return True
```

`window_s` is 5s. But the real 5m/15m/30m bookings are **0.5–1.2s apart** — they
are *inside* that window, so they fell through to the exact test, which rejects
them because they differ by one tick. Measured on the live book:

```
cross-timeframe pairs <= 20s                        717
  gap <= 5s (i.e. INSIDE the exact window)          711   <- clause never ran
  gap  > 5s                                          16   <- clause could run
```

The clause fired on 16 of 727 pairs. A fix that can only execute where the
defect does not occur is a no-op with a comment.

Its own tests were red — all three of them (`removed == 1` for a one-tick pair,
for the 22s pair, and for the three-timeframe fixture). They had been left
failing in the working tree.

## Defect 2 — the window came from a fixture, not from the book

`MULTITF_WINDOW_S = 20.0`, justified as "40x the largest observed gap", with the
fixture claiming the largest gap was 22s. Both numbers were hand-written. The
measured distribution is:

```
matched cross-timeframe pairs              n=705
  p50                                       0.54s
  hard maximum                             36.1s    <- ABOVE the 20s window
next pair of any kind                    357.7s     <- one scan cadence
```

So the constant was below the very maximum it claimed to cover, and the test
that "documented" 22s could never have passed against a 20s window. This is
rule #11 in reverse: the fixture encoded the assumption instead of the data.

## Fix

1. The two clauses are **independent siblings**, not a fallback chain:

```
(1) EXACT     — same entry AND same pnl, within window_s
(2) CROSS-TF  — different known timeframes, entries within 0.1%,
                pnl within $0.05, within MULTITF_WINDOW_S
```

   Evaluated separately; either may merge a pair.

2. `MULTITF_WINDOW_S = 60.0`, derived from the **empty band** in the measured
   distribution: bookings top out at 36.1s, the next honest re-entry is 357.7s.
   60s sits in that gap — 1.7x above the real maximum, 6x below the nearest
   legitimate re-entry.

3. The stale fixtures now derive their gaps from the constant
   (`MULTITF_WINDOW_S + 60`) so they cannot silently drift out of agreement
   with the code again.

## Effect — it MOVES verdicts

```
                      before      after
paper rows collapsed  525         734
paper NET             -2.18       -3.33
edge 'validated'      -0.13 net   -0.66 net   <- now correctly DECAYING
edge 'breakout'       18 trades   17 trades
live population PF    1.05        1.03        (+2.33 net, above 1.02 floor)
```

`validated` was previously inside the 2026-10-17 "blind spot" (WR between the
45% demote floor and the 58% promote bar) and drew no verdict. On the corrected
book it is net-negative and is now flagged. The live/dead PF split from
2026-10-22 continues to hold: the live book pays (PF 1.03 > 1.02 floor), so the
aggregate line stays quiet while the whole book reads 0.96 — which is exactly
the pooling defect that rule #15 removed.

## Lesson

**When adding a clause to an existing key, verify it is REACHABLE in the regime
the defect lives in.** A nested `if` that is only entered on the *other* side of
a threshold is untested by construction, and its tests will be the only thing
that ever notices.

**And derive thresholds from the measured distribution, not from a fixture you
wrote.** A hand-written fixture restates your assumption; it cannot contradict
it. Look for an *empty band* between the defect cluster and the nearest honest
event, and put the constant inside that band.
