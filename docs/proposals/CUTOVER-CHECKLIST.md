# Cutover checklist — paper to real money

**This is a gate, not a plan.** Each stage has a checkable exit condition. Nothing
proceeds until the previous stage's condition is met. Owner approval is required where
marked.

Current state as of 2026-10-05: **STAGE 0 running.**

---

## STAGE 0 — top8 hold

| | |
|---|---|
| state | **RUNNING** (started 2026-10-05 11:01 UTC) |
| action | none — automated |
| driver | cron `v2-scale-gate-check` (`eb407360fa04`), 09:00 + 21:00 UTC |
| exit condition | **2 full days with all 7 gates passing** |
| owner input | none (only contacted on failure) |

Gates, both lanes: stale decisions <1% per coin · step_ms_max <15000 · stale_ms_max <
ceiling · book_prefetch hit ≫ stale_refetch · jev used <70% of cap and refused 0 ·
jev_ok/jev_calls >=90% · cycle_error 0 and watchdog problems {}.

On any failure the cron edits `V2_SYMBOLS` back to `top3`, restarts both lanes, reports.

---

## STAGE 1 — all15

| | |
|---|---|
| state | **NOT STARTED — needs owner go-ahead AND Stage 0 complete** |
| action | edit `V2_SYMBOLS=all15` in `config/v2.env`, restart `v2-paper v2-paper-b` |
| exit condition | **2 full days with all 7 gates passing** |
| owner input | **REQUIRED — explicit go-ahead** (per the rollout prompt: "ONLY after 2 clean days on top8 and the owner's go-ahead") |

Same gates, same auto-rollback. Rollback target on failure: `top3`.

**Why the 2 days matter here specifically:** 15 symbols x 2 lanes is the load the box has
never carried. The gates are what would catch step time or snapshot staleness going bad.

---

## STAGE 2 — V2 must actually trade

| | |
|---|---|
| state | **NOT STARTED — this is an OBSERVATION, not an action** |
| problem | **V2 has filled ZERO trades.** 100+ cycles evaluated, every outcome `ignored`, `v2paper` chain 0 events |
| exit condition | **V2 fills trades in paper and they behave like the model expects** |
| owner input | none — nothing to authorise |

**This is the real blocker and it is not on the critical path of any decision made so
far.** V2 is a working decision engine that has never decided to trade. Paper P&L from
the *other* book says nothing about V2 until V2 trades.

**Both outcomes are findings:**
- it trades and behaves -> the V2 line is validated
- it never trades -> the strategies may be too conservative to ever act, which is a
  design problem, not a tuning problem

**Flag to the owner if still at zero trades after ~1 week.**

---

## STAGE 3 — the stop-width question

| | |
|---|---|
| state | **BLOCKED — needs ~1 month of `worst_px` data** |
| action | re-run `scripts/replay_stop_sweep.py` against real drawdowns |
| exit condition | a stop width that survives the §6 falsification criteria |
| owner input | none yet |

`worst_px` shipped 2026-10-05 (`b3f38e8`). Before it, three separate analyses were
attempted on inferred data and none could answer the question — see
`docs/proposals/stop-width.md` §8.

---

## STAGE 4 — RR finding, forward

| | |
|---|---|
| state | **NOT STARTED** |
| action | forward-test the RR>=2 hypothesis on trades placed after the finding |
| exit condition | the direction holds out-of-sample in forward paper |
| owner input | none yet |

The RR effect validated 4/4 on historical data (`docs/proposals/rr-floor.md` §6) but is a
**proxy** for stop width, not a cause. See `docs/proposals/stop-width.md` §7.

---

## STAGE 5 — small real money

| | |
|---|---|
| state | **NOT STARTED** |
| exit condition | **ALL of:** |
| | 1. V2 trading in paper and behaving like the model |
| | 2. the stop-width question answered |
| | 3. the RR finding survived a forward test |
| | 4. 90+ days of live book with a measured edge |
| owner input | **REQUIRED** |

**If reached: SMALL. Kill switch armed. Watched daily for the first week.**

**Honest timeline:** 3-4 weeks earliest, 1-3 months realistic. **No date is promised**,
because the binding constraints are data accumulation (Stage 3) and an unproven engine
(Stage 2), not effort.

---

## Standing rules

- Live execution stays disabled (`config/LIVE_ENABLED*` retired) until Stage 5 is
  explicitly approved by the owner.
- No order-path change lands on an unvalidated belief. Two such changes were reverted
  in the week to 2026-10-05.
- Exit 0 != success. Verify the outcome, not the status code.
- Anything not measured cannot be declared safe. The `worst_px` gap is the worked
  example: the field needed to answer the stop question was never recorded.
