# Learning: a dedupe key that ignores half its fields silently deleted 24% of the paper book

**Date:** 2026-10-14
**Cron class:** LEARNING
**Commit:** `self-improve: dedupe the paper book on trade identity, not (symbol, entry)`

## What was found

`scripts/self_improve.py` deduped both books with

```python
k = tuple(str(t.get(x, ""))[:16] for x in ("symbol", "entry", "exit", "close_ts_utc"))
```

On the **real** book the key works: real records carry `exit` + `close_ts_utc`.
On the **paper** book it is catastrophic. Measured from `data/unified-paper.json`:

```
records with `exit` set            :    2 of 2941
records with `close_ts_utc` set    :    2 of 2941
```

So for 99.93% of paper records both fields fall back to `""` and the key
**degenerates to `(symbol, entry)`** — and the `[:16]` truncation of an ISO
timestamp collapses the remainder to minute resolution. Every trade ever opened
at the same price on a symbol, **months apart, with different TP/SL and different
outcomes, merged into one.**

```
raw paper closes          2941
after the code's dedupe   2229        <- 712 real trades (24%) DELETED
gross P&L hidden          +7.138
```

Worked examples straight out of the book:

```
OP/USDT @ 0.1223  6 distinct trades, 2026-09-19 -> 2026-10-09
                  sl 0.1223 / 0.1238 / 0.1199 ; pnl -0.0981 ... +0.2330
                  -> collapsed to 1
LINK/USDT @ 13.165  one TP win (+0.1185, Sep) + one TIME loss (-0.0004, Oct)
                  -> collapsed to 1
```

## Why it matters: it changed the SAMPLES every decision reads

Step 3 of `self_improve.py` builds `by_edge` from **`paper_c`** — the truncated
set — and the disable list it writes is honoured by every opener on the live
path (the 2026-10-12 fix). Every per-edge sample was wrong, some by more than
half, and one edge was invisible:

```
edge          TRUNCATED (what the rule saw)   FULL (truth)      sample err
supertrend    n=127                            n=191             -34%
loose         n=53                             n=83              -36%
validated     n=56                             n=72              -22%
rsi2          n=5  (below n>=15 floor ->        n=27  wr=56%     invisible
                    never judged at all)
momentum      n=24                             n=30              -20%
```

⚠️ **Honest scope of the decision impact — stated precisely, because the
tempting version is stronger than the truth.** On the **GROSS** basis the
truncation flips signs (`loose` −0.264 -> +0.144, `momentum` −0.183 -> +0.192).
But the gate has judged on **NET** since 2026-10-13, and at NET **every edge is
still a loser** on both sets — the ~0.16% round trip sees to that. So the
truncation did **not** flip a NET-basis disable verdict: it changed the *size* of
each sample and the *magnitude* of each verdict, and it hid `rsi2` entirely
below the n>=15 floor. The concrete harm is therefore:

1. **`rsi2` could not be judged at all** on the truncated set (n=5) though the
   book holds 27 trades of it — the gate was blind to a 27-trade edge.
2. **Every surviving n was understated 20–36%**, so the gate was reasoning from
   samples a third smaller than reality — exactly the sample-size error that
   makes a marginal verdict look decisive.
3. **The reported book statistics were wrong by 24%** (see the table below),
   which is what the owner reads.
This is a *measurement* defect that propagated into a decision input, not a
demonstrated wrong disable. Say that plainly.

## The fix

`dedupe()` now defaults to a trade **identity** — `symbol + open time + entry +
size` — the same shape the lesson loop already uses
(`symbol|ts_utc|entry|size|pnl`), and it no longer truncates. An explicit `keys`
argument is still honoured but used verbatim, so the minute-resolution
truncation cannot come back through the front door.

Result on the live books:

```
paper  raw 2941 -> deduped 2941   (0 false merges)
live   raw   37 -> deduped   37
```

And the headline the card reports changes materially — **the book is about half
as bad as it was being described**:

```
                 OLD (buggy)      CORRECTED
paper n            2213             2941
paper gross      +12.112          +18.881
paper NET         -4.340           -2.808
EV / trade      -0.00196         -0.00095
```

## The generalisable lesson

**A key is only as good as its presence on every record it is applied to.** The
same `(symbol, entry, exit, close_ts_utc)` key was correct on one book and
meaningless on another **in the same function call**, because the two books do
not share a schema. A dedupe whose fields default to `""` does not fail loudly —
it silently becomes a different, much coarser key.

Two disciplines this argues for:

1. **Before trusting a dedupe count, assert the key is UNIQUE and complete.**
   Here `(symbol, ts_utc)` is unique on all 2941 records and `(symbol, entry,
   exit, close_ts_utc)` is not — that one-line check is the whole diagnosis.
2. **A field that is `None` on 99.9% of a population is not "sparse", it is
   ABSENT** — conditioning on it is conditioning on a constant. (Same family as
   the `best_px` pitfall in `trade-lesson-loop` and the guard-condition pitfall
   in the NULL-vs-missing note.)

## Verification

- `tests/test_self_improve_dedupe_identity.py` — 4 tests: two same-price trades
  with different open times survive; a true byte-identical re-booking is still
  removed; the **old** key is pinned as collapsing the distinct pair; live
  `size_usdt` records do not collide.
- A/B against the pre-change file: **baseline 3 failed / 1 passed -> with-fix 4
  passed**. The 3 baseline failures are exactly the new tests, proving they pin
  the behaviour rather than the source string.
- Full suite: **607 passed, 3 skipped**, 0 failures.
- Live re-read through the patched module: `paper NET n=2941` (was 2213),
  `gross +18.881` (was +12.112).

## What is NOT claimed

- This does **not** mean the paper book is profitable. At NET it is still
  **−2.808** / EV −0.00095 per trade. The book loses less than reported, it does
  not win.
- It does **not** re-enable `momentum`. On the NET basis the gate still flags
  **every** edge, `momentum` included. This commit only stops the gate from
  judging on a sample 20–36% too small and from being blind to `rsi2` (n=5
  truncated vs n=27 true). Any re-enable is a separate, reviewed change to
  `data/signals-state.json`.
- I did **not** find a NET-basis verdict that the truncation flipped. Stating one
  would have overstated this. The finding is a *measurement* defect with a large
  magnitude (24% of the book), not a demonstrated wrong disable.
- The corrected `n=2941` is the count the *rule* will now see; whether any edge
  is promoted or kept is still the gate's decision.
