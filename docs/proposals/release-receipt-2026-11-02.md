# The E9 repair left a receipt that no code can write

2026-11-02 · found while verifying E9 on the first H4 bar after the fix.

## What the verification found first (E9 is live and correct)

The 2026-11-01 answer to E9 said the lag "can only be confirmed closed on a pass
where a NEW H4 bar exists". The first such pass was 2026-11-01T16:07Z:

```
16:07:59  ETH_TURTLE_H4        hold   in_pos:true    <- still the OLD code path
18:07     (post-2ac046e live; no line: bar already consumed)
20:07:05  ETH_TURTLE_H4  symbol_owned_by_other_strategy owner=RANDOM_ENTRY_CONTROL
20:07:05  ETH_TURTLE_H4  action none  in_pos:false  bar 20:00   <- evaluated
00:07:09  ETH_TURTLE_H4  symbol_owned_by_other_strategy owner=RANDOM_ENTRY_CONTROL
00:07:09  ETH_TURTLE_H4  action none  in_pos:false  bar 00:00   <- evaluated
```

Both arms are at bar 2026-11-02T00:00 in state; the treatment is flat, the
control keeps the claim, and the treatment still reaches `decide()` — which is
exactly the invariant 2ac046e adopted. The exchange (demo, private read) holds
ONE 1.2 ETH position, avg 2241.49, stop 2192.37, uPnL +40.49 — matching the
control's state, not a second position. **E9: closed, verified on live data.**

## The defect: `released_unowned_position`

The state file carried, on the released arm:

```json
"released_unowned_position": {
  "entry": 2241.36, "symbol": "ETHUSDT",
  "why": "phantom adoption by a --dry-run pass that persisted state
          (pre-2026-10-31 fix); released again 2026-11-01 and the PAIRING
          EXEMPTION that let it re-adopt is itself fixed in 4xx (E9).
          Owner is RANDOM_ENTRY_CONTROL."
}
```

1. **No code writes it.** `released_phantom_claim` in `run_once()` sets
   `in_pos/stop/qty/entry/symbol/ledger_id` and nothing else; `strat_state()`
   never creates it; `git log --all -S` finds no commit that ever introduced it.
   It was written by hand during the 2026-11-01 session.
2. **It contradicts the log.** There is no `released_phantom_claim` row anywhere
   in `decisions.jsonl` (0 occurrences in 379 rows). The 11-01 release happened
   on the 20:00 bar (close 2258.42); the receipt says 2241.36, which is the
   10-31 snapshot price. "fixed in 4xx" names a commit that does not exist.
   The JOURNAL's "post-fix replay → released_phantom_claim" was a replay against
   a temp state, not the live pass — the live repair was done by script.
3. **It is unbounded.** Neither the release path nor `strat_state()` refreshes
   or prunes it, so the field would still say "released 2026-11-01" after the
   next release, or after a buy of a different symbol.

Severity is bookkeeping, not trading: the arm's *trading* state was correct
throughout. But a receipt that no code can produce and no test can read will be
believed by the next human who asks whether this arm can be trusted — the same
failure mode as a green test that encodes the bug (2026-11-01).

## Fix (`scripts/run_crypto_trend_demo.py`)

- The release path pops `released_unowned_position` when it releases a claim, so
  legacy receipts clear on the next release and no new one can survive.
- A `dry` pass logs `would_release_phantom_claim` instead of the real event. A
  rehearsal still writes no state (`not dry: save_state`), but it is no longer
  silent about the repair it would perform. The event name matches the existing
  `would_buy` / `would_close` convention.
- The event row now carries `dry` and is dated by `bar`, as every other row is.

The live receipt was removed from state.json after the fix; the control's
committed claim and the exchange position were not touched.

## Evidence

- RED first: `tests/loop/test_release_receipt_and_reentry.py` → **2 failed, 2
  passed** on `git show HEAD:scripts/run_crypto_trend_demo.py` (dry announcement,
  receipt clearing). GREEN after: 4 passed.
- Mutation checks: deleting the `pop` re-fails `test_a_released_arm_carries_no_
  receipt_field`; hard-coding `released_phantom_claim` re-fails
  `test_a_dry_pass_announces_the_release_it_would_make`.
- The other two tests pin the invariant the release must NOT break: a released
  arm can buy its own symbol after the owner exits, and that entry carries a
  committed `ledger_id` (otherwise the next pass would release it as a phantom).

## Lesson

**A repair that exists only in a state file is a repair with no receipt.** The
decision log is the audit surface because it is append-only and written by the
pass that acted; any field the state carries must be (a) written by shipped
code, (b) refreshed or removed on every transition that invalidates it, and
(c) false when a test reads it. Verify the state against the log, not against a
narrative written the same evening as the fix.
