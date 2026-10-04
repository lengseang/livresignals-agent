# V2-16: accuracy round 2 (entry edge + scorecard) and production readiness

Status: design, committed BEFORE any round-2 result was seen. The study below is pre-registered.

## 1. Where accuracy is actually lost (from V2-15's evidence)

1. **Entries carry no measured edge.** Exits cannot fix that: every exit rule lost money on 2025-26 when the
   entries were naive.
   * The gateway's model saw only minute-to-4h features of 3 coins. Its AUC was 0.49.
2. **Jev decides blind to anything older than 4 hours.** `features-v1` has returns of 1m-4h, plus an hourly
   ATR/RSI/EMA. Multi-day trend, cross-sectional strength and the BTC regime are absent. These are where the crypto
   literature finds persistent effects (time-series and cross-sectional momentum).
3. **Nobody grades the decisions.** Jev's and the gateway's probabilities are logged and never scored against what
   happened. "Accuracy" is unmeasured.

## 2. Part A: the scorecard (measure accuracy continuously)

`trading.v2.scorecard` reads the cycle chain and the price history.

* For every logged decision with a snapshot, it labels what happened next:
  * the forward return at +1h, +4h and +24h;
  * whether a long would have hit the approved +7% before -3.5% within 14 days (the gateway's label).
* For each source (jev, gateway primary, gateway dual-run) it reports:
  * the action mix;
  * the hit rate of directional calls;
  * the Brier score and calibration of P(LONG) and `expected_edge` against the barrier label;
  * the agreement matrix between Jev and the gateway.
* Position advice gets its own score: was the advised EXIT better than holding to the bracket? That is measured
  from the same bars.
* Pure and deterministic. A decision whose horizon has not elapsed is "pending", never guessed.

## 3. Part B: entry-edge study v2 (pre-registered)

**Data.** Binance spot 1h klines for the 15-coin universe already used by the repo's shadow lab, 2020-01 to
2026-08. Coins enter when their history starts (SUI 2023, ARB 2023).

**Decisions.** Long only (spot). One position per coin. A decision point every 4h (UTC) while flat.

**Barrier.** It is volatility-scaled, because a fixed 3.5% is half an ATR on SOL and two on BTC.

* stop = clamp(1.5 × ATR14(daily)/close, 1%, 5%), and target = 2 × stop.
* Horizon: 7 days.
* Costs: 0.10% fee plus 1.41 bp per side (the approved paper model).
* Exit management: the shipped bracket with the lock-half ratchet. It is simulated on 1h bars, stop first.
* The fixed 3.5/7 barrier is reported as a secondary.

**Features** (closed bars only, computed at the decision point):

* Returns over 1, 3, 7, 14 and 30 days, each divided by daily vol × √days.
* Close vs the 20-day and 50-day EMAs; the 20-day EMA slope.
* Donchian(20d) position; RSI(14) on daily bars.
* ATR%, and the 7d/30d volatility ratio.
* The 7d and 30d return RANK across the coins available at that instant; the coin's 7d return minus BTC's.
* BTC's 30d return, and whether BTC is above its 50d EMA.
* 7d quote volume divided by the 30d average.

**Candidates.** Six, fixed now:

* F1 TSMOM: the 30d and 7d returns are both > 0.
* F2 XS: the 30d rank is in the top third.
* F3 BREAKOUT: the close is above the prior 20d high.
* F4 REGIME: F1, and BTC is above its 50d EMA.
* F5 REVERSION: daily RSI < 30.
* M: L2 logistic regression on all the features, refit each year on an expanding window. The L2 strength is chosen
  on the last train year. It enters when P ≥ break-even + 0.05.

**Walk-forward out-of-sample years:** 2023, 2024, 2025 and 2026 (to August). The pooled OOS period is
2023-01..2026-08. 2020-2022 are only ever training or warm-up data.

**Admission bar** (each candidate, pooled OOS). All four must hold:

* mean net return per trade > 0;
* the week-block bootstrap lower bound > 0 at the 1 − 0.05/6 level (Bonferroni over the 6 candidates);
* positive mean in at least 3 of the 4 OOS years;
* for M, Brier skill > 0 against the expanding-window base rate.

Reported for every candidate, admitted or not:

* trades, win rate, mean, sum, profit factor and the per-year means;
* the "every decision point" benchmark;
* each coin's buy-and-hold over the same span, for context.

**If a candidate is admitted:**

* Its features become `features-v2` context: a new, versioned block that Jev SEES (more evidence, better answers)
  and the gateway uses.
* The gateway's entry cap may be lifted for that rule only. Paper only.

**If none is admitted:** the report says so, and the entry side stays as it is. Jev still gets the daily context
block, because more evidence cannot make an honest answerer worse. That claim gets checked by the scorecard.

## 4. Part C: production readiness (paper, resident on the VPS)

"Production" here means the V2 paper runner as a supervised, resident service on the VPS. That is the Notion
note's shadow → paper → live ladder at its paper step.

* Real-money execution is NOT built. Nothing in the evidence supports it: there is no measured entry edge, and
  fees and slippage are unknown.
* A written live-readiness gate (section 5) states what would have to be true first.

What gets built:

* **`trading.v2.preflight`:** checks the Python version and dependencies, that the workflows are wire-ready, the
  gateway model's integrity, that the key is PRESENT (never printed), reachability of the Binance data API and
  TypeSafe, that the output directory is writable, disk headroom, that every existing chain verifies, and that the
  approved config loads.
  * Exit codes are 0 for ok, 1 for a failed check and 2 for usage. A failed preflight blocks the start.
* **`trading.v2.notify`:** Telegram through the box's existing `hermes send` CLI.
  * It never raises: a notification failure must not break a trade.
  * It rate-limits by event key.
* **`trading.v2.watchdog`** (cron, every 5 min): checks heartbeat age, kill-switch state, fallback share, disk and
  chain integrity. It alerts on each transition (not every run) and can never touch the runner except by alerting.
* **`deploy/v2/`:**
  * a SEPARATE pm2 ecosystem file for the V2 apps. The existing `ecosystem.config.js` is not touched or started;
  * a wrapper that loads the key from a 600-permission env file;
  * logrotate-friendly output;
  * `max_memory_restart`.
* **`docs/v2/RUNBOOK.md`:** start, stop (the STOP file), kill (the V2_KILL file), verify, rollback, rotate the key,
  read the heartbeat and the scorecard.
* **`docs/v2/VPS-AGENT-PROMPT.md`:** the prompt the owner pastes to the VPS agent. It syncs the release with
  fast-forward only, runs the tests and preflight, deploys the V2 apps, verifies them and reports back, under the
  repo's hard rules.
* **Release:** the tag `v2.0.0-rc1` on the release branch. The VPS deploys the tag, not a moving branch.

## 5. Live-readiness gate (what must be true before real money; NOT met today)

1. At least 30 days of paper running with 0 cycle errors and chains that verify.
2. A positive, statistically supported paper expectancy, or an admitted entry rule plus paper consistent with it.
3. Real fees and slippage measured, from testnet or tiny live fills.
4. An execution adapter with reconciliation (order state, partial fills, exchange-side OCO for the bracket),
   plus its own tests.
5. An owner decision in writing, plus `LIVE_ENABLED`, which V2 never touches.

## 6. Adversarial self-review

* *Six candidates means multiple testing.* Hence Bonferroni, a per-year consistency requirement, and a test window
  that never trains.
* *1h bars blur the intrabar order.* Stop-first assignment biases against the candidates, so any edge found is
  conservative.
* *Survivorship.* The 15 coins are today's liquid set, so the past is flattered. This is stated in the report, and
  it biases toward FINDING edge. That is one more reason the bar is strict.
* *The resident process could leak memory or fill the disk.* There is a memory cap, a disk check in preflight and
  the watchdog, and chains are append-only by design (no rotation of ledger files; their growth is measured and
  reported).
* *The VPS agent could run the wrong pm2 file or force-push.* The prompt names the exact commands, forbids the old
  ecosystem file and force operations, and asks for evidence (test counts, preflight output, heartbeat) before it
  claims success.
