# The aggregate PF verdict had no window — a good month hid a losing one

**Date:** 2026-10-23
**Component:** `scripts/improvement_loop.py` (rule 3), new `_recent_live_pf`
**Tests:** `tests/test_improvement_pf_window.py` (7 tests)

---

## What was wrong

Rule #15 (2026-10-22) fixed *which edges* the aggregate profit-factor line may
ask: the LIVE ones, not the seven disabled edges that had not filled in 5
weeks. That was correct and it holds.

It did not fix *what period* it asks them about. `_split_live_pf` sums win and
loss dollars over **every trade the live edges have ever written**. So the
headline "is the book paying after costs?" was a lifetime mean.

Measured on the real book, 2026-10-23:

```
live cohort (loose, validated)   n=2183   net +2.20   PF 1.03
    2026-08   n=  14   net  +0.07   PF 1.10
    2026-09   n=1267   net  +6.94   PF 1.17      <- the good month
    2026-10   n= 902   net  -4.81   PF 0.82      <- the month we are in

weekly PF:  1.53  1.27  0.75  0.75  1.20  0.50   <- still falling
last 28d:   n=1198  net  -6.26   PF 0.83
```

`PF_NET_FLOOR` is **1.02**. The pooled figure is **1.03** — one basis point
above the bar — so the loop drew **no exit advice at all**, while the last four
weeks lost **$6.26** and the weekly series was monotonically deteriorating over
the last three weeks.

The September the cohort earned in is not a market the book can trade in. The
mean was not wrong; it was answering a question nobody asked ("has this cohort
paid over its life?") and its answer was being read as ("is it paying now?").

## Why the existing guards did not catch it

* **Rule #12** requires a verdict to state the window it was measured on. Every
  *per-edge* verdict got a window via `edge_liveness` (#14). The aggregate was
  the last figure in the report with no time bound — this proposal applies #12
  to it.
* **Rule #15** split the population by liveness but left the period unbounded.
* **Rule #9** ("re-derive at write time") does not help: the pooled number is
  re-derived every run, correctly. The defect is the population, not staleness.

This is the same shape as #15: a rule that fixed one dimension of a cohort
(edges) while the other dimension (time) stayed pooled. A guard is only as good
as the axes it was written on.

## The fix

`_recent_live_pf(trades, edge_meta, days=PF_WINDOW_DAYS)` computes the net PF of
the LIVE edges over the last **28 days** (four scan-cadence weeks), from raw
trades (which carry `ts_utc`; edge rows do not).

Rule 3 now fires when **either** the pooled live book **or** the 28d window is
below `PF_NET_FLOOR`. When the window is what fired, the line states **both**
figures and the window:

```
NET profit factor 0.83 < 1.02 over the LAST 28d window (1198 trades, -6.26 net)
on the live edges (loose, validated); the same cohort reads 1.03 over its whole
life (2183 trades, +2.20 net). The book is LOSING NOW and the lifetime mean is
masking it. ...
```

Design choices, each with a reason:

| Choice | Why |
|---|---|
| 28 days | 4 weekly cycles: long enough that one quiet week cannot flip the verdict, short enough to describe the current market. |
| Same `PF_MIN_LIVE_N` floor | A thin window must not manufacture advice any more than a thin cohort may. |
| Window does **not** replace pooled | The pool is real evidence about the design's life. Suppressing it would be #12 in reverse. Both are shown, the window decides. |
| Undated records excluded **and counted** | A record that cannot be placed in time must not silently date to "now" and vote. |
| Fires on the window even if pooled is fine | That is the defect being fixed: pooled 1.03 / window 0.83. |
| Absent window data → old behaviour | A broken reader degrades to the pre-fix pooled rule rather than muting rule 3, and claims no window. |

## Verification

* Full suite: **757 passed, 3 skipped** (750 before, +7 new, no regressions).
* Real entrypoint (`scripts/improvement_loop.py`) now writes `recent_pf` into
  `edge_meta` and emits the `rr_tune` line above.
* Old-behaviour proof: removing `recent_pf` from `edge_meta` returns the loop to
  **0** `rr_tune` lines on the same book — the window, not the code churn, is
  what surfaces the loss.

## What this does NOT claim

This is a **reporting** fix. It does not change sizing, entries, or any live
order path. It makes the loop say out loud that the live paper book is losing
inside its current window — which it previously could not say.

## Follow-up (not done here)

* `loose` last filled **2026-10-11** (>14d). Under #14 it will be reclassified
  NOT LIVE within days, which will drop it out of the live cohort and change
  the windowed PF. The window will follow the cohort automatically; no action
  needed, but expect the live cohort to shrink to `validated` alone.
* The three paper records surviving collapse with `pnl: None` and no `exit`
  (all 2026-09-05: BTC + 2×ETH — these are the report's "3 uncosted") are
  counted as `0.0` by every reader, i.e. silently booked as scratch trades in
  the win/loss denominator. Verified: `net_of` returns `(0.0, False)` for each.
  They should be dropped at collapse time rather than priced at zero — a
  separate, smaller defect (~0.1% of the book, no PF impact at this size).
