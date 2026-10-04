# V2-15 results: in-position management, the System One gateway, audits

Branch `v2/position-management` (stacked on `v2/approved-limits`). Design and pre-registration:
`docs/loop/designs/V2-15-position-management-and-gateway.md`, committed BEFORE any result was seen.
Paper only. Nothing here can place a real order.

## 1. The owner's problem, and the root cause in the code

> "Sometimes the indication is a designated TP, but the signal fails to reach it, and the trade returns to
> losing while the position is still on hold, going to SL instead of TP."

* **Root cause:** `target_pct` was computed and then dropped. No position could take profit.
* **A second cause:** exits needed the ENTRY confidence ladder (0.90). Fallback could never exit, so with Jev
  down, the only way out was the stop (E5).

## 2. What was built (three layers; each can only reduce risk)

| layer | what | when | needs Jev? |
|---|---|---|---|
| L0 bracket | `target_price` on RiskDecision/ExecutionCommand; paper takes profit (stop first if both touched in a bar) | resting | no |
| L1 ratchet | once MFE ≥ 50% of the target distance, stop ≥ entry + 50% of MFE; applied from the next bar; a level already through the price becomes an exit at the next open | every 1m bar | no |
| L2 advice | Jev's POSITION workflow (HOLD / TIGHTEN_STOP / REDUCE / EXIT + momentum_fading, reversal_risk, target_reachable), given the position in R units; on any Jev failure the gateway answers | every decision cycle while positioned | no (gateway) |

* **Contract K, `position_adjustment`:** a stop amendment that can only tighten. A long's stop only rises; it
  never reaches or crosses the price.
* **Position policy:** EXIT and REDUCE need ≥ 0.70, and TIGHTEN needs ≥ 0.60. This is the same for Jev and the
  gateway, and it closes E5. These thresholds are UNVALIDATED defaults (see E13).
  * TIGHTEN moves the stop to the last price − 1.5 × ATR, and only if that is tighter.
  * EXIT and REDUCE are reduce-only proposals that go through the risk engine.

## 3. Evidence: exits (BTC/ETH/SOL 1m, train 2023-01..2024-12, test 2025-01..2026-08)

Costs per side: 0.10% fee + 0.41 bp half spread + 1 bp slippage. Stop 3.5%, target 7%.

**Pre-registered study** (12 rules, chosen on train, test touched once): `docs/v2/evidence/exit_study_{train,test}.json`

* Train chose R0 "stop only, hold ≤ 14 days" at +2.3% per trade. That is the 2023-24 bull-market drift.
* On test it ranked 9th of 12 (-0.33%).
* **On test, every rule lost money with naive entries.** No exit rule creates an edge that the entries don't have.

**Paired analysis of the owner's exact case** (POST-HOC, labelled): `docs/v2/evidence/exit_study_paired.json`

Same entries, only trades that reached HALF the target. 6,727 train trades and 5,424 test trades.
Week-block bootstrap CIs are relative to holding the bracket.

| from half-way, do... | train mean | train loss rate | test mean | test loss rate | test diff vs hold (95% CI) |
|---|---|---|---|---|---|
| hold the bracket (before) | +3.84% | **26.8%** | +3.56% | **28.3%** | – |
| take profit at half | +3.28% | 0% | +3.28% | 0% | -0.28% [-0.83, +0.32] |
| breakeven stop | +3.71% | 0% | +3.40% | 0.2% | -0.15% [-0.48, +0.21] |
| **lock half the MFE (shipped)** | **+3.82%** | **0%** | **+3.53%** | **0.2%** | **-0.03% [-0.51, +0.45]** |
| ATR trail k=3 | +3.71% | 4.3% | +3.42% | 3.0% | -0.13% [-0.59, +0.36] |

**Conclusion.** More than 1 in 4 half-way trades used to end as losses. Lock-half cuts that to ~0% at no
measurable cost to the mean, in both periods. Taking profit early costs expectancy, which was significant in
train.

**Reproduce:**

```
python -m trading.v2.studies.exit_study --data <klines root> --cache <dir> --out docs/v2/evidence --phase train|test|paired
```

## 4. Evidence: the System One gateway (`docs/v2/evidence/gateway_study.json`)

* **Model:** L2 logistic regression on the 14 bar-derived features-v1 numbers, predicting "+7% before -3.5%
  within 14 days".
  * Fit on 2023, with the L2 strength chosen on 2024; refit on train; tested once.
* **Result: no skill.**
  * Long test AUC is 0.494 and Brier skill is -0.003.
  * Short test AUC is 0.532 and Brier skill is +0.003.
* **The pre-registered admission bar FAILED.** The gateway stays below EXECUTE for entries. An entry from it
  escalates and does not trade.
* **Honesty mechanism:** the probability the gateway uses is shrunk toward the base rate by the measured
  out-of-sample Gini (2·AUC − 1). That is 0 for long.
  * An unskilled model is not allowed to sound sure. A test caught "LONG at 0.77" before this fix.
* **What the gateway shares with Jev:**
  * the same nine questions, answered with probabilities, from the same snapshot;
  * the same road (signals, risk_level and questions all reach the policy, so the demotions apply);
  * the same "NO_TRADE/HOLD unless clear" default.
* **Its other answers** are formulas scaled by train-period quantiles. They are labelled as heuristics.
* **"Rival Jev" is MEASURED, not asserted.** The gateway dual-runs on every Jev cycle, for entries AND position
  advice. Agreement is in the cycle records and in `heartbeat.json` (`dual_run`, `dual_run_advice`).
* **What this says about Jev:** Jev's own entry skill has never been measured. In every live entry cycle so far
  it said NO_TRADE. The study shows these features carry no 7%/3.5% edge, which is consistent with that.
* **Fail-over chain:** Jev → gateway → strategy vote (last resort).

**Live** (real jev-1.13.0, test key from the environment only): 3/3 position-workflow calls OK (256-288 ms,
~1,630 input tokens). The case was a hypothetical long at 60% of the way to target on BTC, ETH and SOL. Jev said
HOLD (0.75 / 0.85 / 0.75), with target_reachable 0.62-0.63. The gateway also said HOLD on all three.

## 5. Audits (each finding has a test that fails when the fix is reverted)

| # | finding | fix |
|---|---|---|
| 0 | target computed and dropped; exits gated like entries; fallback could never exit | L0/L1/L2 above |
| 1 | a restarted runner built a FRESH paper book while its chain continued, so positions, cash and stops vanished | `PaperBook.restore` replays the verified chain through the same accounting; refuses a broken or unreconcilable chain; never-filled orders are logged `cancelled: restart` |
| 2 | no whole-chain invariants for position management | 60 hostile seeds (random/malformed advice, smuggled entry actions, shocks), with coverage floors |
| 2b | partial closes recorded as "exit" | labelled "reduce" |
| 3 | exits were not compared with the gateway; the advisor did not count the Jev model | position advice dual-run + `model:<name>` counter |
| 4 | an over-confident unskilled gateway | OOS-skill shrinkage |

* Mutation checks: 4 on the paper book, 3 on the advisor, 2 on the study simulator, 1 on the pipeline routing.
  All were caught.
* Legacy `src/trading/risk/` and `scripts/` are untouched: the diff against `main` is empty.

**Tests:** `pytest tests/ --continue-on-collection-errors` gives 1322 passed, 6 skipped and 10 errors. That is
444 baseline + 878 V2. The 10 errors are the old E2 `src/trading/data/` gap.

## 6. Still open (owner)

* **E13:** the position thresholds (0.70 / 0.70 / 0.60, 1.5 ATR) are unvalidated. Shadow data will show how
  often Jev's exits beat holding.
* **E14:** should an engaged kill switch still allow REDUCE/EXIT orders? Today `contracts.md` rejects every
  order. Stops and the ratchet keep protecting regardless.
* **E15:** a better entry signal is the real bottleneck. Neither the gateway's features nor (so far)
  measurably Jev show an entry edge.
