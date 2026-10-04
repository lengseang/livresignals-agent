# V2-4: risk engine v2 (Contract G)

Source: owner's note layer 6 ("Completely deterministic... never ask an LLM") and `docs/v2/contracts.md` G.

## Problem
Contract G lists seven checks; the legacy layer (`src/trading/risk/`) enforces more (weekly/drawdown halts,
2% risk per trade, 5% max stop, exposure cap, strategy breakers) and returns notional, not a base quantity.
A G with only seven checks, or a rewrite, would silently drop real safety properties.

## Choice: wrap, never replace
- Limits are `GUARDS` (read-only, re-validated on every call with `validate_guards`; weakened -> reject).
- Sizing is `compute_position` unchanged; V2 converts notional -> base quantity, **rounding DOWN** (rounding to
  nearest overshot the exposure cap by ~0.0005 USD in testing; found by the first test run).
- Halts are `check_portfolio_breakers` called three times with the other two terms neutralised (so each of
  daily/weekly/drawdown is reported on its own line) and `check_strategy_breakers`.
- New V2 checks: spread, duplicate order/position, exchange health, kill switch, short policy, symbol match.
- Every check is listed, spec seven first, then extras. `reject_reason` = first failed in that order.
- Unknown exchange health / kill-switch state fail closed. Any internal exception -> rejected decision with
  all seven required checks failed plus `engine_error`.
- Reduce-only intents are not blocked by entry limits (loss limits, exposure, spread) but ARE blocked by the
  kill switch, exchange health and duplicate-order checks. Owner to confirm (E7).
- SHORT entries are rejected unless `RiskLimits.allow_short` (GUARDS: spot only).
- `daily_loss_cap_usd` has no default: the live cap is the owner's number; None = explicitly no absolute cap.

## Failure modes / adversarial self-review
1. *A legacy limit changes and V2 keeps its own copy.* There is no copy; tests assert `R.GUARDS is GUARDS` and
   `R.compute_position is legacy.compute_position`, and a weakened-guards test rejects.
2. *Day-start/week-start/high-water inputs are wrong or stale.* They are required inputs from the caller (V2-6
   must source them from the paper ledger); the engine cannot know. Zero history = honest zeros, not guesses.
3. *The 2%-risk / 2%-stop combination sizes to 100% of equity* (existing behaviour, e.g. equity 10,000 -> notional
   10,000). Not changed here; worth the owner's attention when exit params are chosen (E7).
4. *Kill switch blocks exits*: a stuck switch could trap a position. Matches the note's "BLOCK NEW ORDERS"; the
   protective stop placed with the entry is the backstop. Owner decision (E7).
5. *Spot-only vs Contract G's `max_leverage`.* Legacy forbids leverage; the check is `1x` unless GUARDS allows.
6. *Latent legacy bug seen while reading*: `breakers._utc_week_start` raises on Tue-Sun early in a month
   (`day.replace(day=day.day - weekday)`). Unused today; V2 does not call it. Escalated (E6), not edited.
