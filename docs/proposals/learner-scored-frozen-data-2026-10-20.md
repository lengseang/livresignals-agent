# paper_learner published a verdict about TODAY from data frozen 65 days ago

**Date:** 2026-10-20
**Status:** FIXED (provenance now printed) — the underlying stale-input problem is OPEN
**Component:** `scripts/paper_learner.py` (cron `0 10 * * *`, daily)

---

## 1. What the learner printed

Every daily run prints a single verdict line:

```
paper-learner: searching param space (72 combos)...
  best found (NET of fees): dip=-0.08 rsi<=30 red>=4 acc=56.3% net_ev=+0.184% n=907
paper-learner: kept current (best net_ev +0.184% not >= current +0.184% + 0.05pp)
```

That line reads as a statement about the current market. It is not.

## 2. What it was actually measured on

`_load_all_series()` reads `data/full_sweep.json` (deleted by disk_cleanup),
then falls back to `data/live_fine.json` and `data/alts_fine.json`.
Both fallbacks were last written **2026-08-15** — 65 days before this run.

| Source | Window | Series |
|---|---|---|
| live_fine.json | 2023-01-01 → 2026-08-15 | BTC 15m/30m |
| alts_fine.json | 2023-01-01 → 2026-08-15 | ETH, SOL 15m/30m |

Meanwhile the live candle store (`data/trading.db`) carries BTC/ETH/SOL
15m through **2026-10-20** (`reports/data-health.json`: 9.56M candles, ok).

So the system holds fresh data and scores its parameter grid on a
seven-week-old snapshot. Nobody could see that from the output: the
verdict named a config, an accuracy, an EV and an n — but never the window.

## 3. Why it matters beyond one report

The verdict feeds `reports/improvement-report.json` → `self_improve` →
`night_tuner`. Each of those reasoned about "the sharp-dip entry is
negative-EV after fees" as though it were a fact about today. It is a fact
about Jan 2023 – Aug 2026, and the most recent quarter of the book — the
part where a decayed edge would show first — was never in the sample.

## 4. Re-derived on current data (this run)

Rule 9 — re-derive at write time, never quote. Scored the adopted config on
the DB's own 15m candles, last 90 days:

```
config           sym         90d_n   full_n  full_ev%
(-0.08, 30, 4)   BTC/USDT        0      406     +0.429
(-0.08, 30, 4)   ETH/USDT        0      756     +0.580
(-0.08, 30, 4)   SOL/USDT        0      886     +0.397
(-0.08, 45, 4)   BTC/USDT        0      627     +0.141
(-0.04, 40, 3)   ETH/USDT       57     5796     -0.001
(-0.02, 30, 3)   BTC/USDT       76     5176     -0.071
```

**The adopted config fired ZERO times in the last 90 days.**

That is not decay — it is silence. A −8% drawdown combined with RSI ≤ 30
and four consecutive red candles is an event that more or less stopped
happening in the recent regime. The config shows a great full-sample EV
(+0.43% to +0.58% per trade) because the whole edge lives in the 2023–2026
sample's handful of crashes.

Trade frequency on BTC 15m since 2025-03-01, straight from the DB:

```
(-0.08, 30, 4)   n= 16    ~9.8 trades/yr   ev=+0.219%
(-0.04, 30, 4)   n=149    ~90.9 trades/yr  ev=-0.044%
(-0.02, 30, 3)   n=834   ~508.9 trades/yr  ev=-0.141%
```

A config that trades ~10 times a year cannot be validated by a daily
learner, and at a $15 account with a $3-ish risk unit it cannot pay a
monthly number either. The learner has optimised itself into a corner where
the winning row is untestable in the time available.

## 5. The fix shipped today (provenance only)

`_load_all_series()` now records, for every series it returns, the file it
came from and that file's mtime. `main()` prints it under the verdict:

```
  INPUT: 6 series from alts_fine.json, live_fine.json — last updated 65d ago (frozen history, NOT live)
```

Properties, all pinned by `tests/test_paper_learner_provenance.py`:

* the stamp is the file's **real mtime**, re-read each run (rule 9) — a test
  touches the file and asserts the stamp moves;
* the stamp is keyed by the **series name actually scored**, not the raw key
  in the file, so the `15m` → `15m:BTC/USDT` remap cannot orphan it;
* the age is derived, never asserted — the line reads `65d` today and will
  read `80d` next week without a code change (rule 11: no hardcoded
  direction in a derived verdict).

Also fixed alongside: `_load_all_series()` did `x[1]` on every element
*before* its try/except, so a source storing bare closes (`[float, ...]`)
raised `TypeError: 'float' object is not subscriptable` and took down the
whole grid. Both shapes are now normalised explicitly.

Mutation-checked: swap the stamp back to a constant and
`test_stamp_reflects_the_file_not_a_constant` fails; restore and it passes.

## 6. What is NOT fixed — the real problem

The learner still scores frozen files. Printing the age makes the defect
visible, it does not remove it. Three options, in order of preference:

1. **Score from the DB** (`data/trading.db`), which is fresh and already
   holds 5m/15m/30m/1h/4h/1d for 8 symbols. `_load_all_series()` is the only
   function that would change; `simulate()`/`evaluate_params()` take plain
   close lists and are unaffected. This is the correct fix and makes the
   fallback files redundant.
2. **Regenerate the sweep files** nightly before the 10:00 learner run.
   Cheaper, but creates a second copy of data that can drift from the DB —
   exactly the class of bug this project keeps finding.
3. Do nothing and rely on the new `INPUT:` line to warn the reader. Only
   defensible if someone actually reads the log, which they do not.

Recommendation: **1**. It also unblocks the open "time-of-day filter"
exploration, which needs intraday timestamps the current loader throws away.

## 7. Second-order finding for self_improve

`reports/improvement-report.json` currently emits, at priority 1:

> Paper win rate 43.0% below 55% — the capitulation edge may be decaying.

but the learner's own grid says the adopted dip config fired **zero** times
in 90 days. A win rate on a book that is 1376/2577 made of the `loose` edge
is not evidence about a config that did not trade. Before any `edge_decay`
suggestion based on paper win rate is promoted, it should be gated on that
edge having actually traded inside the window being judged — the same
"count the denominator" error as rule 7's RR band and the single-valued
regime cohort (n=251 of 2577).
