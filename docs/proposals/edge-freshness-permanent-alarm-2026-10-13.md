# Design: the per-edge freshness guard can never clear (a permanent false alarm)

**Date:** 2026-10-13 · **Status:** implemented · **Class:** measurement integrity

## Problem

`improvement_loop._edge_freshness()` raises the note

> "per-edge stats are NOT current: 499/2923 trades tagged (17.1%), last tag
> 2026-10-12T18:16:38 (0.8d ago). Edge promote/demote verdicts below are STALE
> — read them as historical, not as the live book."

whenever `tagged_pct < 50 or stale_days > 2`.

On 2026-10-13 10:40 the report carried **both** `"stale": false` (last tag
0.8 days old — the feed is *fresh*) **and** that STALE note. The report
contradicts itself, and the note is wrong.

## Root cause — the denominator

`tagged_pct = tagged / len(all closed trades)`. The book holds **2923** trades,
but **2751 of them (94%) are LEGACY**: written *before* the 2026-10-10 fix that
made the two active openers stamp `edge`. Those trades can never acquire a tag —
they are immutable history. So `tagged_pct` is bounded above by

```
(taggable trades) / (taggable + 2751 legacy)
```

which **shrinks every day the book grows**, and is already 17.1% — permanently
below the 50% threshold. The guard fires on every run, forever, no matter how
healthy the live feed is.

Measured on the real book (2026-10-13):

```
population            n     tagged   tagged %
pre-2026-10-10 fix   2751      362      13.2%   <- can never be tagged
post-2026-10-10 fix   172      137      79.7%   <- the live feed: healthy
whole book           2923      499      17.1%   <- the guard's (wrong) input
```

The staleness half of the guard works (`stale_days` is real and current); only
the `tagged_pct` half is broken.

## Why this matters (it is not cosmetic)

This is the codebase's own documented failure mode: *"a guard that reports
success when it checked nothing is the same bug one level up"* and, from
`check-improvement-consumed.py`, *"noise gets ignored."*

A priority-1-class warning that is **always on** is indistinguishable from no
warning. When the feed genuinely does freeze — the exact defect the 2026-10-10
patch existed to make loud — the operator sees the same 17.1% note they have
seen every day and ignores it. The guard masks the event it was built to catch.

## Options

1. **Drop the `tagged_pct` clause entirely.** Simple, but loses the genuine
   case: a fresh batch of untagged trades (e.g. a new opener that forgets to
   stamp) would go unnoticed. Rejected.
2. **Measure tagging on a rolling recent window** (e.g. trades in the last 7
   days). Correct in spirit, but introduces a second time-window constant and
   is sensitive to quiet periods (a `?`-day window with 0 trades).
3. **Measure tagging only on trades the current writers could have tagged** —
   i.e. exclude trades older than the tagging fix. Honest, but hard-codes a
   date into the loop.
4. **Keep the whole-book `tagged_pct` as a reported total, but gate the
   staleness note/verdict on the RECENT tagging rate.** The guard's question is
   "is the feed still being fed?", which is a *current* question; legacy history
   is irrelevant to it.

## Choice

**Option 4**, with the recent window measured from the **newest untagged
trade** (inclusive):

- `tagged` / `tagged_pct` stay as the whole-book figures (honest totals, and
  they are what prior readers/tests keyed on).
- add `recent_tagged` / `recent_tagged_pct` / `recent_n`: the tagging rate over
  the window from the **newest trade that slipped through untagged** onward.
  That boundary is the last point at which tagging demonstrably failed, so every
  legacy trade — which lies strictly before it — is excluded, while a fresh
  untagged trade lands *inside* the window and counts against the rate. It is
  detected data-drivenly, not by a hard-coded date, so it self-maintains if the
  tagging scheme is ever reset.
- If the window is smaller than `MIN_RECENT` (30) trades — a lone untagged trade
  at the very top, or a quiet stretch — the guard falls back to the whole-book
  rate (the pre-fix behaviour), so it can only ever be *stricter*, never falsely
  quiet.
- the note and the `stale` flag fire when the judging rate < 50 or
  `stale_days > 2`. The freshness half is untouched.

Measured on the real book after the fix: `recent_n = 138`,
`recent_tagged_pct = 99.3`, `stale_days = 0.8`, `note = ""`. The permanent false
alarm is gone; the genuine `stale_edge_data` suggestion no longer fires.

### Why not an earliest-tag or fixed-time window

Two alternatives were tried and rejected on the real book:

- *Earliest-tagged boundary*: the pre-2026-10-10 era contains 362 tags from the
  now-DEAD imported strategies (they stopped firing 2026-09-13), so "earliest
  tag" lands in that dead era and the window swallows the whole legacy base
  (measured: 17.2%).
- *Fixed time window* (7/14/30 days): the book's trade *rate* changed at the
  fix — thousands of legacy trades back-fill a calendar window (7d → 28.8%,
  14d → 13.7%). The legacy base is old in trade-order but not necessarily old in
  calendar time. Rejected.

## Failure modes (adversarial self-review)

**"It is 3 months later and this failed: why?"**

- *Empty recent window.* If no trade has been tagged since the boundary, the
  recent set degenerates. Handled: when `recent_n == 0` the recent rate is
  undefined and the guard falls back to the whole-book rate — the pre-fix
  behaviour, so it can only ever be *more* strict, never falsely quiet.
- *First tag is very recent.* If the very first edge-tagged trade is today, the
  "recent" set is tiny and a single untagged trade swings it. Mitigation: the
  `stale_days` clause still guards the genuinely-frozen case, and the report
  prints `recent_n` so a tiny denominator is visible. Not worth a second
  threshold.
- *Boundary drift.* If an old trade is later back-filled with an `edge` tag,
  the boundary moves *earlier*, admitting more legacy trades and lowering
  `recent_tagged_pct` — i.e. the guard gets stricter, not looser. Fail-safe
  direction.
- *Time-zone / bad stamps.* Unchanged from today: the existing `try/except`
  around stamp parsing is preserved; a bad stamp cannot crash the loop.

## Test plan

`tests/test_improvement_loop_freshness.py` gains:

1. `test_healthily_tagged_book_is_not_flagged_stale` — a book shaped like the
   real one (a large legacy untagged era, one transition trade, then a
   continuous fully-tagged era) must report `stale is False` and `note == ""`.
   This is the exact real-world case that is broken today; it FAILS on the
   pre-fix code (mutation-checked: reverting the judging input to `tagged_pct`
   makes it fail).
2. `test_recent_untagged_tail_still_flags` — legacy history PLUS a recent
   untagged tail must still set the note (the guard keeps its teeth).

The two existing stale tests (`test_edge_freshness_flags_stale_feed`,
`test_edge_freshness_ok_when_recent_and_dense`) pass unchanged. Full suite:
**586 passed, 3 skipped** (584 + the 2 new tests).
