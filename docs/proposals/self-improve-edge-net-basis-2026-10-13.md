# Learning: the disable list was written on a basis the book doesn't settle in

**Date:** 2026-10-13
**Cron class:** LEARNING
**Commit:** `self-improve: judge per-edge disables on NET, not the flattered gross`

## What was found

On 2026-10-11 the improvement layer was fixed so that per-edge
promote/demote verdicts read **net** P&L, not the flattered gross
(`tests/test_improvement_net_basis.py`). That test's own docstring names the
two decision-makers it fixed — `improvement_loop` and `night_tuner` — and
says the cost basis "was propagated to unified_trader and self_improve."

It was not. `scripts/self_improve.py` **step 3** still read gross:

```python
st = stats(ts)                                          # sums GROSS pnl
if st["n"] >= 15 and st["wr"] < 42 and st["pnl"] < 0:   # GROSS verdict
```

That step is not cosmetic: it **writes** `data/signals-state.json` → `disabled`,
and `unified_trader` **honours** that list on the real-money path
(`_edge_disabled`, lines 476–495 and 546–548). So a gross-vs-net misjudgment
here directly decides whether a losing edge keeps trading real money.

## The measurement

The measured round trip is ~0.16%, 5.4x the old 0.03% constant. On the live
book (2026-10-13) the two bases disagree:

```
edge       n   gross_wr  gross_pnl | net_wr  net_pnl   old -> fixed
loose     53     37.3%      +0.14  |  34.9%   -0.35    -   -> DISABLE
validated 40     38.9%      -0.12  |  29.6%   -0.43    -   -> DISABLE
momentum  24     40.0%      +0.19  |  33.3%   -0.05    -   -> DISABLE
```

Three edges whose true edge is thinner than the fee were being **kept alive**
by the gross figure. `loose` is one of the two active openers — the busiest
losing edge in the book.

## The fix

`stats_net_of()` — a small reader over `paper_net.net_of` — and one call-site
swap. Net is the verdict; gross is carried in the message so the haircut stays
visible (`net=+X, gross +Y`), matching the pattern used elsewhere in the file.

Verification: `tests/test_self_improve_edge_net_basis.py`, **mutation-checked**
(reverting `stats_net_of` → `stats` makes the wiring test FAIL). Full suite
**584 passed, 3 skipped**. End-to-end on the real book: no spurious state
change (the losing edges were already disabled), the gate now agrees with them
by construction rather than by accident.

## The generalisable lesson

**A fix applied to "the decision-makers" is only complete if you enumerate
them from the *write* sites, not from memory.** The 2026-10-11 patch listed
its consumers in a docstring and named two; there were three, and the omitted
one held the real-money pen. The tell is in the same codebase: the fix's own
comment claimed `self_improve` was covered while `self_improve`'s disable gate
still called the gross `stats()`. When you propagate a basis change, grep for
every writer of the *output* (here: `signals-state.json`), not for the ones you
remember — the disable list is only as honest as the least-honest gate that
feeds it.
