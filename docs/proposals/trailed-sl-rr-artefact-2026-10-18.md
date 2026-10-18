# Learning: `sl` is the trail, not the risk — the whole RR finding was measuring its own ratchet

**Date:** 2026-10-18 (overnight shift)
**Cron class:** LEARNING
**Files:** `~/hermes-home/scripts/trade_lesson_loop.py`,
`tests/test_lesson_trailed_sl.py` (new), `data/trade-lessons.jsonl` (repaired)
**Scope:** lesson/verdict layer only. No order path, no parameter, no strategy, no
paper book touched. `unified-paper.json` and `ensemble-paper.json` opened read-only.

---

## 1. What was found

`_rr()` computed reward:risk from the **closed record's** `sl`:

```python
risk = abs(e - sl);  return abs(tp - e) / risk
```

But `unified_trader.py:346-350` (the paper manage loop) **overwrites `p["sl"]`**
once a trade is green:

```python
if pnl_pct >= 1.5 and p.get("sl", 0) < p["entry"] * 1.012:
    p["sl"] = round(p["entry"] * 1.012, 4)     # trail to +1.2%
elif pnl_pct >= 0.5 and p.get("sl", 0) < p["entry"]:
    p["sl"] = round(p["entry"], 4)             # lock breakeven
```

There is **one** `sl` field. The trail writes the current stop over the designed
stop, so on any trade that ratcheted the closed record no longer describes the
trade that was entered.

Measured on `data/unified-paper.json` (3,041 closed with pnl):

```
sl > entry (trailed)    675    95.3% win   net +70.27
sl < entry (intact)   1,557    24.7% win   net -61.11
sl == entry (breakeven) 815    50.4% win   net  +9.32   <- _rr() returned None
whole book            3,047    47.2% win   net +18.48
```

**The entire book profit sits in the records whose geometry was rewritten, and
27% of the book was being labelled `UNKNOWN` by the zero-risk denominator of the
breakeven lock.** All 815 `UNKNOWN` lessons in the unified book had `rr: None` —
the two numbers reconcile exactly.

`sl` is conditioned on the outcome: the trail only fires *after* the price rose.
This is the `best_px` watermark pitfall, in a field the skill listed as safe.

## 2. The fix

`_rr()` now returns the **designed** RR. Each entry function hardcodes a target
multiplier on a fixed stop, so the RR it designs is a constant:

```
_paper_open             stop=s    tp=c(1+2.0s)  -> 2.0
_pullback_open          stop=.012 tp=c(1+1.5s)  -> 1.5
_paper_loose_open       stop=.012 tp=c(1+1.5s)  -> 1.5
_imported_strategy_open stop=.02  tp=c(1+1.8s)  -> 1.8
```

- `_designed_rr()` — when the stored stop is at/above entry it is a ratchet, so
  the lane constant is used. Untouched records still read their own geometry.
- `_rr_stored()` + `geometry()` — expose **both** numbers, so the conflict is
  visible rather than silently corrected.
- `UNKNOWN` is now reserved for genuinely unrecorded geometry.

**Validated on the 1,551 records whose `sl` was never touched: 93.9% agree with
the lane constant to within 0.02 RR.** The 6% residual is 4dp price rounding
(2.04 vs 2.0) — the case `RR_TOL` already exists for.

## 3. The ledger was stale, so it was repaired

Lessons are append-only and idempotent, so a fix to `_rr()` only ever reaches
*new* trades — and `threshold_validity()` reads `rr` straight off the lessons
file. Added `--repair` / `--repair-dry-run`: recomputes only the derived fields
(`rr`, `verdict`, `verdict_note`, `why`, `what_would_improve`). Identity fields
(`id`, `ts_utc`, `book`, `symbol`, `trade_ts_utc`, `pnl`, `exit_reason`,
`session`) are never rewritten.

```
repair: 3,171 lessons, 1,783 changed, 0 unmatched
ALIGNED -> GOOD OUTCOME, POOR DECISION      447
UNKNOWN -> {AS DESIGNED/GOPD/ALIGNED/GDPO}  815
AS DESIGNED -> GOOD DECISION, POOR OUTCOME   63
verdict flips as predicted, identity violations 0, order preserved True
```

## 4. The headline it overturns

```
STORED sl :  rr>=2  n=942   WR 74.4%  net +58.28    rr<2  n=1284  WR 25.4%  net -49.11
DESIGNED  :  rr>=2  n=471   WR 53.9%  net  +7.60    rr<2  n=1755  WR 44.0%  net  +1.57
```

The "RR>=2 carries all the net, RR<2 loses money" result — the strongest
selection finding in `trade-lesson-loop` §2, double-confirmed across paper and
real — **was measured on trail-mutated geometry.** 471 trades labelled "inside
the winning population" were designed at RR 1.5 or 1.8.

`threshold_validity()` has flipped from `ok=False / first_paying_band=2.4` to
**`ok=True / first_paying_band=2.0`**.

### 4b. And the honest correction — RR does not separate the book either

The first reading of the repaired ledger said "RR<2 is net-positive, do not cut
it." Splitting by half **does not support that**:

```
               rr>=2                rr<2
FIRST  HALF   n=417  +4.353       n=1103  +9.947
SECOND HALF   n=402  +5.385       n=1119  -1.201
```

RR<2 is positive only in the first half. **The defensible claim is the weaker
one: RR does not separate this book.** Not "RR<2 is good", not "RR<2 is bad" —
the variable carries no stable signal once the trail is removed. The rollup text
says exactly that; it originally said "net-POSITIVE" and was corrected before
shipping, because a full-sample aggregate on a cohort is the most flattering
number available and this file has a history of four wrong calls made that way.

## 5. Verification

```
RED first        : 5 failed, 2 passed
GREEN            : 16 passed (7 new + 9 boundary)
mutation         : old _rr() restored -> exactly 4 behaviour tests fail
full suite       : 654 passed, 3 skipped  (647 baseline + 7 new)
repair dry-run   : writes nothing (ledger md5 unchanged, verified)
repair           : 1,783 changed, 0 unmatched, 0 identity violations
idempotency      : second run, md5 UNCHANGED, --quiet stays silent
UNKNOWN          : 815 -> 0 in the unified book
```

## 6. Lesson

**A field that is overwritten in place is not a record of the state it is named
for.** `sl` reads as "the stop" on every schema in this repo. It is "the last
stop the manage loop happened to write," and for 1,490 of 3,047 trades that is
a ratchet level the trade never took risk at. The skill had already written the
rule for `best_px` — *a watermark is recorded when a trade did well and absent
when it did not* — and did not apply it to `sl`, which is the same shape one
layer down: **the trail is a watermark on the price, and it rewrites the stop.**

Second: **a nested idempotent ledger needs a repair path, not just a fix.** The
code was correct for 40 minutes before the ledger was, and in that window the
rollup published the old verdict with full confidence. `--repair-dry-run` first,
then `--repair`, is now the pattern for any change to a derived field on an
append-only file.

Third, the same caution as last shift: **the corrected finding is smaller and
weaker than the one it replaces**, and the temptation is to over-read it in the
new direction. The trail is *not* shown to help; the stop-width and RR
questions are now **unmeasured rather than answered**, because the trail
destroyed its own record. Restoring judgeability is not the same as finding an
edge.

## 7. Not claimed

- The trail is **not** shown to help or hurt. Its record is what was destroyed.
- The stop-width finding in §2 of the skill (`wide stop disables the ratchet`)
  measured `SL`-exit cohorts; those cohorts are partly the trail's, so it needs
  re-derivation on designed geometry. **Not done.**
- No parameter was changed. `RR_GOOD` stays 2.0.
- The real book has the same defect (4 of 37 live, 6 of 21 futures) but its
  lessons are not written by this loop, so only paper was repaired.
