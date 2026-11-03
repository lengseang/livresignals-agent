# The incumbent is not a challenger

**2026-11-03 · autonomous improvement pass.** One defect, found by running the
suite on a freshly refreshed cache; no order path, no threshold, no disable
list, no state file touched.

## What happened

`pytest tests` was **red on arrival**: `1 failed, 837 passed` at
`test_research_loop_coin_fragility.py::test_the_solo_challenger_is_measured_even_when_it_has_no_family`
(`assert 6 == 1`). That test asserted the crown's `sma_r` family has exactly
one member **on live data**. The 2026-11-03 01:00 cache refresh re-ranked the
grid and moved the crown from `sma_r=100` (solo) to `sma_r=150` (six siblings).
That is the #11 shape — a test pinning a ranking, red for a change that is not a
defect — so it was fixed first (skip when the crown has siblings; the
multi-variant coverage belongs to the sibling test).

Chasing *why* the crown moved is what found the real defect.

## The defect: `lead_is_coin_fragile(best, live)` with `best is live`

`_change_lines()` scores a candidate "NEW BEST" by calling
`lead_is_coin_fragile(best_row, live_row)`. `best_row` is
`max(rows, key=score)` and the LIVE config **is one of the rows**. So on any
run where the incumbent is still best — or merely *ties* a challenger, because
`max()` returns the first and LIVE is second in the grid — `best_row is
live_row`, `lead` is `0.00` by construction, and all three refusal triggers
fire on themselves:

| trigger | why it fires on the incumbent |
|---|---|
| (a) `live_band[0] <= 0 <= live_band[1]` | the live median is essentially always inside its own band |
| (b) `lead - drop_max <= 0` | `0 - drop_max` is ≤ 0 for any `drop_max > 0` |
| (c) `seg_lead_frac < 0.75` | a row cannot out-earn itself in any 150d segment → `0.0` |

On the 2026-11-03 grid the verdict was literally
`fragile: True, lead +0.00pp, "dropping DOGE alone cuts the challenger's median
26.79pp -> lead -26.79pp; only 0% of 150d segments prefer it"` — about
`sma_r=150` vs `sma_r=150`.

Two consequences, both visible in what the loop talks about:

1. **The contradiction returns by the front door.** `live_fragile` is asked of
   the same row and is normally `False`, so the run would emit *"REFUSED 'NEW
   BEST' {live config} … the incumbent stands"* and, in the same message,
   treat that config as the incumbent. The 2026-10-24 fix stopped the two
   branches contradicting each other; it did not stop one branch contradicting
   itself.
2. **A tie is reported as a refusal.** Because `max()` prefers the first row,
   an exact tie always crowns the challenger, which is then refused for
   "beating" the incumbent by 0.00pp. The reader sees a refusal of a config
   that is numerically identical to the one that stands — a verdict produced by
   the order of a list.

Neither reached Telegram on 2026-11-03 only because the refresh made the
incumbent score *higher* than every challenger, so the NEW BEST branch was not
entered. It is reachable on any refresh that flips the ranking back.

## Fix

* `lead_is_coin_fragile()` computes `is_incumbent = chal is live_row` and, for
  that row, emits **no** refusal reason. The three triggers stay exactly as
  they are for a real challenger. `live_fragile` is unchanged and still
  answered for the incumbent — it is the only question that row can answer.
* `_tied_challengers()` makes an exact tie on `(pos_folds, median)` a
  **stated tie** ("No config change is implied"), not a refusal. Reachable path
  only; a non-tie never enters it.
* `_fragility_line()` puts the findings file's sentence behind the same
  distinction: the incumbent's line says *"the grid crowns LIVE itself — no
  challenger to test"* and states its band instead of *"best vs LIVE +0.00pp;
  holds"*, which reads as a head-to-head that never ran.

## Evidence

* RED first: 4 new tests failed on HEAD (`is_incumbent` absent; two
  `KeyError: 'cfg'` from fixtures that were not grid rows).
* GREEN: `tests/test_research_loop_coin_fragility.py` **19 passed, 1 skipped**.
  Full suite **845 passed, 4 skipped, 0 failed** (arrival baseline: 837 passed,
  1 failed).
* Four mutations, each caught:
  1. remove the incumbent guard → the incumbent-is-refused tests fail;
  2. exempt on `lead == 0` instead of identity → the point-tie challenger test
     fails (it must still be refused);
  3. drop the tie branch → the tie test fails;
  4. treat "tie" as equal fold counts only → the 2026-10-19 noise-lead test
     fails.
* Reachability on the real grid (`scripts/research_loop.py`, 01:07 and 01:11
  UTC): `best median 34.26% (4/4) | live median 34.26%` → **no change (silence
  by design)**. Before the tie branch the same run printed
  `NEW BEST config: {sma_r: 150} …` — crowning the incumbent as its own
  challenger.

## What this pass did NOT claim

The loop's numbers are **not** a live-book result. The harness runs on
`research/price-cache-1000d.json` (1000 daily bars × 25 coins, refreshed 01:00
today), the book is a regime L/S simulation with no fees and no shared-account
constraint, and the LIVE row itself is currently **above its own
leave-one-coin-out ceiling** (`34.26%` vs band `[+7.47, +30.20]`). That is a
regime break flag on a simulation, not a profit statement, and it is the same
"above the ceiling" state the 2026-10-20 entry recorded. Nothing here promotes
a config; the incumbent stands on its own merits, not because a guard stopped
looking.
