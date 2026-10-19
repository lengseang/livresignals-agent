# The ensemble "fix the geometry" lever no longer pays, and drop−DOGE kills the challenger

**Date:** 2026-10-19 (overnight shift)
**Supersedes in part:** `ensemble-geometry-measured-2026-10-17.md`
**Context:** the ensemble book carried an `instrument_warnings` entry on two
consecutive shifts. Per the lesson-loop rule, the third shift measures instead of
re-reporting. Two measurements below, both replay, both costed at 0.28% round
trip on a $5 position.

---

## 1. 🟡 HONEST LIMIT OF THIS MEASUREMENT, READ FIRST

The replay uses the **staircase** construction: every `hold`-th bar is an entry,
segments do not overlap. The real runner enters on a **momentum signal** and
opens at most one position per ensemble at a time, and 132 trades over ~24 hours
on a 5m/48-bar lane implies effective **overlap** — the book opens faster than a
non-overlapping 48-bar staircase allows.

Measured baseline drift:

```
book   (ensemble-paper.json, 132 closed)   per-trade  -0.01927
replay (staircase, 3.0/2.0, 5m)            per-trade  -0.00563
replay (staircase, 3.0/2.0, 30m)           per-trade  -0.00573
```

⇒ **The staircase is kinder than the book by ~$0.013/trade.** Per the lesson
loop's own rule ("a simulation that cannot reproduce the baseline's own number
has not validated anything — use only its ORDERING"), **only the ordering below
is claimed.** The absolute levels are not.

A signal-conditioned replay (momentum rule fires → enter next bar, allow
overlap) is the correct instrument and **has not been run.** That is the
follow-up, not a conclusion.

---

## 2. The claim that closed the ensemble finding on 10-17 no longer holds

The 2026-10-17 proposal decided the ensemble defect with this line:

> "fixing the geometry RESOLVES the exits (573 TIME instead of 1800) and STILL
> LOSES."

Staircase replay, same construction, longer window (5m from 2026-08-09, 30m from
2026-10-01), same cost:

```
5m  hold=48                      per-trade
  current 3.0/2.0   T385/788   -0.00563
  fix     1.2/1.0   T310/713   -0.00801   <- fix is WORSE
  fix     0.6/0.5   T157/560   -0.00938   <- fix is WORSE
30m hold=12
  current 3.0/2.0   T290/607   -0.00573
  fix     1.2/1.0   T213/530   -0.00792   <- WORSE
  fix     0.6/0.5   T 99/416   -0.01022   <- WORSE
```

(Values from `research/_ensemble_geometry_replay.py`, reproducible.)

**Reachable targets do suppress TIME exits (as claimed) and now cost more per
trade, on both lanes, at both widths.** The 10-17 proposal predicted the
reachable fix would be *less* negative than the current geometry; today it is
more negative at every width on every lane.

⚠️ **Do not read this as "the wide target is good".** It is not: the wide target
still yields **zero TP exits** in the real book and still loses. What changed is
the *alternative*. The honest statement is:

> the ensemble is negative-EV at every geometry tested, with the ordering between
> geometries unstable across windows — so geometry is not the lever, and the
> book should not be cited as evidence about the rule either.

⚠️ And the ordering instability is itself the finding worth keeping: a ranking
that flips sign between 10-17 and 10-19 is a ranking inside noise. **Neither
verdict — "fix helps" nor "fix hurts" — is supported at this sample.**

---

## 3. Live-config claim: drop DOGE and the challenger goes NEGATIVE

From today's research loop run (`reports/research-findings.md`, 13:06):

```
best challenger  sma_r=200   median 27.36%   LOO band [+12.13, +24.48]
LIVE             sma_r=150   median 13.70%   LOO band [ +3.89, +18.78]
lead = +13.66pp  -- but the LIVE config's OWN band reaches +18.78
dropping DOGE alone:  challenger 15.23pp  ->  lead -1.57pp
```

⇒ **The challenger's entire advantage over the live regime config is one coin.**
`regime_trader` is a real-money path (`TREND_REAL=1`, spot, every 4h). The
challenger `sma_r=200` must **not** be promoted on this evidence: its lead sits
inside the incumbent's own leave-one-out band and inverts on a single deletion.

The FRAGILE verdict is already printed by the loop (`FRAGILE — lead +13.66pp
sits inside the LIVE config's own leave-one-coin-out band`). This proposal adds
the **decision** the guard implies: **no promotion; record the DOGE dependency
as the reason.**

---

## 4. What was shipped tonight

| change | verified |
|---|---|
| `trade_lesson_loop.py` `main()` NameError fix | 26 trades learned, idempotent, 29 tests |
| ledger torn-tail repair | 3 torn shapes + healthy no-op |
| wrapper no longer exits 0 on failure | crash path now emits + propagates rc |
| `rr_split` clean-cut guard | live ledger refuses a cut (correct: orderings disagree) |
| research-loop coin-fragility guard | committed with its tests |

`docs/proposals/lesson-loop-main-crashed-2026-10-19.md` has the detail.
