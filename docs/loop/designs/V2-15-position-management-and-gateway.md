# V2-15: in-position management + the System One gateway

Status: design, written BEFORE any study result was seen. The study grid and the train/test split below
are pre-registered here. The test period is evaluated once, after the train-period choice is frozen.

## 1. The problem, read from the code (not assumed)

The owner's example: a trade heads for its take-profit, does not reach it, reverses, and is still held
when it hits the stop. A winner becomes a full loser.

What V2 does today (branch `v2/approved-limits`):

* `propose_trade` computes `target_pct` (7% under the approved limits). **Nothing downstream uses it.**
  `RiskDecision` and `ExecutionCommand` carry no target, and `PaperBook` never takes profit. A position
  ends in one of two ways: its stop is hit, or Jev answers EXIT.
* That Jev EXIT has to clear the ENTRY ladder: confidence of at least 0.90 (EXECUTE). A risk-reducing
  action is gated as if it were adding risk.
* A fallback decision can never EXIT, because the fallback is capped below EXECUTE. That is escalation E5,
  still open. When Jev is down, nobody can close a position except the stop.
* The stop never moves. Nothing remembers how far the trade went in its favour (MFE).

So the owner's scenario is not an edge case. Under today's code it is the ONLY way a winner can end
unless Jev is very sure. That is the gap.

## 2. Layers, fastest to slowest. Every layer can only REDUCE risk.

| layer | runs | owner | needs Jev? | can it add risk? |
|---|---|---|---|---|
| L0 bracket | at fill, resting | risk engine prices it | no | no |
| L1 ratchet | every closed 1m bar | `PositionManager`, pure rules | no | no: the stop only moves toward profit |
| L2 advice | every decision cycle while positioned | Jev, or the gateway when Jev is down | no | no: HOLD / TIGHTEN / REDUCE / EXIT only |

* **L0.** `RiskDecision.target_price` and `ExecutionCommand.target_price` exist. The paper book fills
  the target. A gap through the target fills at the open, which is the better price.
  * Live, this is an exchange-side bracket. It survives the agent being down.
  * When stop and target are both touched in one bar, the STOP is assumed first. This is conservative:
    the bar's path is unknown.
* **L1. A deterministic ratchet on MFE.** Its parameters come from the study in section 4, not from taste.
  * It runs whether Jev is up or not. It is the main answer to the owner's example, because it acts
    within a minute.
  * Jev answers every 15 minutes, which is too slow to catch a fast reversal.
  * Invariant (property-tested): a long's stop never decreases; a short's stop never increases.
  * The stop is never set beyond the last close. That would be an instant exit disguised as a stop.
* **L2. Advice.** When a position is open, the cycle asks a separate POSITION workflow.
  * Questions: `position_action` ∈ {HOLD, TIGHTEN_STOP, REDUCE, EXIT}, plus yes/no `momentum_fading`,
    `reversal_risk` and `target_reachable`.
  * The state gets position features: unrealized R, MFE R, MAE R, minutes held, distance to target in R,
    and distance to stop in R.
  * The answer passes the deterministic bound: TIGHTEN can only raise the stop, by a bounded ATR step.
  * Risk-reducing thresholds are separate from the entry ladder. EXIT and REDUCE execute at 0.70 and
    TIGHTEN at 0.60. These are UNVALIDATED defaults, owner-tunable, and logged.
  * They apply to Jev AND the gateway alike. This closes E5. The "fallback never EXECUTEs" rule exists to
    stop an unvetted source OPENING risk. Closing risk was never its purpose, and blocking it is what
    trapped positions.

The risk engine still prices every order. Reduce-only orders are still never blocked by loss limits,
spread or pauses. The kill switch still blocks new ORDERS. A stop amendment is not a new order, so L1
keeps protecting a position while the switch is engaged.

## 3. The System One gateway: what replaces Jev when Jev is down

Requirement (owner): "the capability need to at least rival Jev's principles and capability".

**Principles it must share with Jev, enforced by construction:**

1. It answers the SAME typed questions. That is the nine entry questions of `crypto_trading_v2` and the
   position questions.
2. It answers with probabilities, not bare labels: a choice comes with a distribution, and a yes/no
   comes with P(yes).
3. It answers from the same evidence: the features-v1 snapshot plus the position.
4. The answer travels the same road. It is a `Decision` carrying probabilities, signals, risk_level and
   questions, so the policy's demotions (entry_quality, EXTREME risk, HOLD/NO_TRADE) apply to it exactly
   as they do to Jev.
5. Every answer is logged with its inputs and a version.

**Capability: honest about what "rival" can mean.**

* Jev's quality on THIS problem has never been measured (it answered NO_TRADE in every live cycle so
  far). "Rival Jev" therefore cannot be asserted. It has to be MEASURED.
* The gateway is computed on EVERY cycle, including when Jev answers (dual-run). The shadow log then
  shows how often the two agree on the action, and later, which one was right.
* Where history can grade an answer, the gateway's number is FITTED and tested out of sample. The main
  case is P(the target is hit before the stop), which drives `expected_edge`, `entry_quality` and the
  action distribution. Out-of-sample calibration is reported with the Brier skill score against the
  base rate.
* Answers that history cannot grade directly (regime label, risk level) are transparent formulas over
  the features. They are documented as heuristics, not passed off as calibrated.

**Authority:**

* The gateway's ENTRY confidence stays below EXECUTE: an entry from it escalates to Hermes and does not
  trade. This holds unless its out-of-sample study clears a pre-registered bar of positive expectancy
  after costs on the test period. A failed study keeps the cap. That is a result, not a defeat.
* Its POSITION answers execute under the risk-reducing thresholds in section 2.

If the gateway cannot compute (missing features), the old strategy-vote fallback still answers. That
is the last resort, and it is what it was before.

## 4. Pre-registered studies (frozen before any result)

**Data.** Binance spot 1m klines (data.binance.vision) for BTCUSDT, ETHUSDT and SOLUSDT, 2023-01 to
2026-09.

* TRAIN: 2023-01-01 to 2024-12-31.
* TEST: 2025-01-01 to 2026-09-30. Evaluated ONCE, after the train choice is frozen.

**Costs.** The approved paper model: fee 0.10% per side, half spread 0.41 bps, slippage 1 bp per side.
Entry is at the next 1m open after the decision. Stop 3.5% and target 7% (approved).

**Exit study.**

* Positions: one at a time per symbol, long only (spot). A decision point every 4h while flat.
* Entry sets, so the ranking is not an artefact of one entry rule:
  * (a) every decision point;
  * (b) only when the features-v1 regime trend is bullish.
* Maximum holding time is 14 days for every rule, so the comparison is bounded.
* Grid (12 rules):
  * R0: stop only. This is today's code.
  * R1: bracket, stop plus target.
  * R2(b): bracket, plus the stop moves to entry plus round-trip costs once MFE ≥ b × target,
    with b ∈ {0.5, 0.75}.
  * R3(b): bracket, plus once MFE ≥ b × target the stop trails at entry + 0.5 × MFE (locks half the best
    excursion), with b ∈ {0.5, 0.75}.
  * R4(a, k): bracket, plus once MFE ≥ a × target the stop trails at the highest high since entry minus
    k × ATR(14, 1h), with a ∈ {0.25, 0.5} and k ∈ {2, 3}.
  * R5(k): no target; a chandelier trail from the start, at the highest high minus k × ATR, never below
    the initial stop, with k ∈ {2, 3}.
* Selection metric on TRAIN: the mean net return per trade, averaged over both entry sets and all three
  symbols. Ties go to the lower giveback rate.
* "Giveback" is the owner's scenario: a trade whose MFE reached ≥ 50% of the target and that closed at a
  loss.
* Reported for every rule, train and test: trades, win rate, mean and sum of net return, profit factor
  and giveback rate.

**Gateway entry model.**

* Label: at each hourly decision point, whether a long hits +7% before -3.5% within 14 days. Ties in one
  bar count as a loss; a timeout counts as not hit.
* Inputs: the bar-derived features-v1 values. The book features are excluded, because history has no
  book.
* Model: L2-regularised logistic regression, standardised on TRAIN. The regularisation strength is chosen
  by a time-ordered split inside TRAIN (2023 fit, 2024 validate).
* TEST report: the Brier skill score against the TRAIN base rate, AUC, a calibration table by decile, and
  the trading result of "enter when P ≥ the break-even probability + margin" under the chosen exit rule,
  compared with entry set (a).
* Admission bar for lifting the entry cap: TEST mean net return per trade > 0 with a 95% bootstrap lower
  bound > 0, AND Brier skill > 0. Fail either and the cap stays.

## 5. Adversarial self-review

* *"The ratchet turns winners into scratches."* Yes, it can. Breakeven stops are known to cut both ways.
  That is why the rule is chosen by the study, and R1 and R0 stay in the grid. If no ratchet beats R1
  out of sample, the shipped default is R1, the plain bracket, and the doc says so.
* *Overfitting the grid.* There are 12 rules, a 2-year train, and a 21-month test that is touched once.
  The selection is averaged over 3 symbols × 2 entry sets.
* *Overlapping trades inflate significance.* One position per symbol, so trades are sequential.
  Bootstrap over trades is approximate, and that is stated.
* *Intrabar order.* Stop-first when both are touched biases the results AGAINST the bracket and the
  ratchet, so any measured benefit is conservative.
* *Jev advice cannot be backtested.* It costs money per call, and history would need a call per cycle.
  It is built, unit-tested, verified live on a call or two, and measured going forward in the shadow log
  against the gateway. It cannot weaken L0/L1: it can only exit earlier or tighten.
* *A gateway exit at 0.70 fires too often.* It is bounded by rules derived from the study, logged, and
  every exit it causes is visible in the comparison stats. The threshold is owner-tunable.
