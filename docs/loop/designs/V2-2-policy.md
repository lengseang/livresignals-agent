# V2-2: Jev workflow definition + policy engine

Source: owner's architecture note ("HER", Notion) layers 4-5 and `docs/v2/contracts.md` E/F.

## Problem
Contract E says thresholds are configured, not inferred, and a fallback can never reach EXECUTE.
The note adds conditions (`entry_quality > 0.70`, `risk != EXTREME`) and three different confidence
cut-offs (0.65, 0.82, and the 0.55/0.75/0.90 ladder). Contract F must never carry a size.

## Choice
- One ladder (0.55 / 0.75 / 0.90, lower-bound semantics). The other cut-offs are not modelled.
- Policy only **demotes**. Each rule lowers the confidence the ladder sees to just below a tier
  boundary (`nextafter`), so `PolicyDecision.tier` stays consistent with `adjusted_confidence`
  (the contract enforces that) and the reason is returned beside it, not lost.
  fallback -> below EXECUTE; HOLD/NO_TRADE -> IGNORE; entry_quality < 0.70 -> STRATEGY_CHECK;
  risk EXTREME -> WATCH.
- `propose_trade` needs tier EXECUTE for every intent (stricter than Contract F, which only requires it for
  entries). One position per symbol: an opposite-side signal is "exit first", never a reversal.
- Stop/target come from injected `ExitParams`. Missing -> no entry. Never a default.
- The Jev workflow is data (`workflows/crypto_trading_v1.json` + `active.json`), validated at load time
  to be able to fill a Decision. Hermes can propose a new one without touching code.

## Failure modes / adversarial self-review ("3 months later this failed: why?")
1. *A demotion rule silently stopped applying.* Each rule has a mutation test (remove the rule -> a test fails).
2. *Fallback reaches EXECUTE through a policy bug.* Defence in depth: the contract rejects a fallback with
   confidence >= 0.90, policy caps it again, and a test builds a forged 0.99 fallback past the contract.
3. *Fallback exits cannot run* (tier <= STRATEGY_CHECK), so a fallback-only outage leaves exits to the
   protective stops placed with the entry. Consistent with the spec, but a real consequence: ESCALATION E5.
4. *Threshold drift.* `policy_version` rides on every PolicyDecision; thresholds are a frozen dataclass, and
   changing them is a code change that goes through the branch/test/backtest/paper gate.
5. *`HOLD` at 0.97 shows up as EXECUTE.* It cannot: non-actionable actions are capped to IGNORE.
6. *Scores wrongly treated as calibrated.* Unverified until shadow data exists; V2-6 logs everything needed to check.

## Test plan
`tests/loop/test_v2_policy.py`: ladder boundaries, every demotion and its no-op case, config honoured,
determinism x1000, position-aware refusals, exit/reduce, mismatched inputs, workflow load/reject.
