# V2-10: deterministic kill controller + Hermes halt requests (Gate 6)

Source: owner's note: Level 5 "Emergency authority" (`HALT_ALL / HALT_SYMBOL / HALT_STRATEGY / REDUCE_EXPOSURE`; "the
deterministic safety controller decides whether the halt is legitimate") and Gate 6 ("exchange disconnect, runaway order
loop, unexpected position, price feed corruption, abnormal slippage, daily loss exceeded -> Kill Controller -> CANCEL
ORDERS -> BLOCK NEW ORDERS. No Hermes required"). `docs/v2/contracts.md` dropped both; today `kill_switch_engaged` is a
boolean someone has to supply.

## Choice
- Contract J `halt_request` (strict, Hermes-only, closed action vocabulary, scope rules).
- `KillController.observe(Observation)` latches on any of the seven emergency conditions. **Unknown required
  observations are triggers** (fail-closed). Exchange needs N consecutive bad readings (so one blip cannot latch it).
  Once engaged only `reset(by=<human>)` clears it. Thresholds live in `KillLimits`, which has **no defaults**.
- `request_halt` can only RESTRICT: it adds a pause that blocks new entries (risk check `halt_scope`; exits are never
  blocked). It cannot engage or disengage the kill switch, place or cancel anything. Requests under
  `min_request_confidence` are rejected. REDUCE_EXPOSURE is only recorded for the policy layer.
- Paper: an engaged kill switch cancels resting paper orders (`PaperBook.cancel_pending`) and blocks every entry.

## Failure modes / adversarial self-review
1. *Hermes spams halts and starves trading.* Pauses are logged, human-resumable, never touch exits; there is no rate
   limit yet: a candidate follow-up if it happens in shadow. Not solved, recorded.
2. *Thresholds guessed.* `KillLimits` is required, not defaulted; the runner/owner must choose (E10).
3. *The kill switch latches on a fluke and nobody notices.* Every transition is a hash-chained, labelled record, and the
   pipeline's `risk_rejected` records name `kill_switch` as the reason.
4. *Audit log blamed the wrong check* (found by the integration test): the context was read before resting orders were
   cancelled, so the first cycle reported `duplicate_order` ahead of `kill_switch`. Fixed by re-reading the context.
5. *Unknown values latching on startup.* The runner must supply every observation from its first call; that is the intent.
6. *Cancel path only matters for resting orders.* Paper market orders fill on the next bar, so the cancel hook is covered
   by a test that plants a resting order; it will matter for real limit orders later.
