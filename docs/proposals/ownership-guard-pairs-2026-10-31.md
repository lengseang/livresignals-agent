# The ownership guard disarmed the treatment, and the dry run re-created the double claim

Date: 2026-10-31 · Follow-up to `294d5ae` ("two strategies claiming one exchange
position") · Files: `scripts/run_crypto_trend_demo.py`, two new test files.

## What 294d5ae got right

The exchange knows symbols, not strategies. A strategy may not adopt a position
another strategy already owns. That is correct and is preserved (test:
`test_unpaired_stranger_still_blocked_from_adopting`).

## Defect 1 — the guard fired before decide(), for every claimant

`294d5ae` placed the guard at the top of the per-strategy body and `continue`d
before `decide()`. Consequences:

- A strategy holding a position could be skipped and never reach its own exit.
- A skipped strategy never advances `last_bar`, so it lags the book silently.

Live evidence (`state/crypto-trend-demo/decisions.jsonl`): from 08:07 UTC
`ETH_TURTLE_H4` logged `symbol_owned_by_other_strategy` on every pass, 10 times,
and its `last_bar` stalled at 04:00 UTC while `RANDOM_ENTRY_CONTROL` reached
12:00 UTC — two closed H4 bars behind and growing.

## Defect 2 — the control suppresses its own treatment

`RANDOM_ENTRY_CONTROL` is the luck baseline for `ETH_TURTLE_H4`: registry field
`match`, same symbol, same exits, same sizing, random entries. When the control
happened to hold ETH, the guard saw "another strategy owns this symbol" and
suppressed the *treatment*. That inverts the purpose of the pair. A matched
treatment/control is one experiment with two arms, not two strategies
contesting one position.

## Defect 3 — a dry run persisted its adoptions

`run_once()` deliberately does not advance `last_bar` when `dry`, with a
comment explaining that a rehearsal must not desync the live pass. It still
called `save_state(st)` unconditionally. Running one `--dry-run` on 2026-10-31
wrote `ETH_TURTLE_H4` into the state file as holding ETH at 2241.49 with
`ledger_id: None` — re-creating the exact double claim 294d5ae had removed, via
the rehearsal path. Verified: state file md5 identical across a dry run after
the fix.

## Defect 4 — nothing stopped a non-owner from exiting

`decide()` returning `close` leads to a market Sell for the whole position on
the symbol. A strategy carrying an unowned position would therefore close the
real owner's position and book the P&L to the wrong arm. The exit path now
refuses unless `ledger_id` (a committed record) or `owns_exchange_position`
proves we opened it, logging `close_refused_unowned`.

## Design

- `match_group(reg, sid)` — the arms of one experiment (self + `match` partner,
  both directions).
- `symbol_owner(st, sid, sym, paired=...)` — who else owns `sym`, ignoring the
  caller's own position and its paired arms. Returns `None` for an owner.
- Ownership now governs **adoption only**: consulted solely on the
  `exch_in and not local_in` branch. An owner always reaches `decide()`.
- `save_state` guarded by `not dry`.
- Exit guarded by proof of ownership.

## Failure modes considered (adversarial self-review)

- *Three months later: two strategies genuinely buy the same symbol.* Still
  allowed — adoption is exclusive, but each may open its own position after
  `place_market`. That is a separate concern (per-strategy sizing vs exchange
  net position) and is noted as a follow-up, not fixed here.
- *A paired arm says it holds a position it did not buy.* The close guard
  catches it: no `ledger_id`, no ownership flag, no Sell.
- *`--status` or another path writes state.* Only `run_once` calls
  `save_state`, now dry-guarded.
- *The live lag test is machine-specific.* It asserts the user-visible symptom
  and skips only when the runner has not polled. It fails until the fixed
  runner takes a real pass — intentional, so the fix cannot be declared done
  from a dry run alone.

## Test plan (RED first)

`tests/loop/test_ownership_guard_pairs.py` (6) and
`tests/loop/test_demo_runner_dry_and_exit.py` (3).

- RED: `test_treatment_does_not_lag_the_book` failed on live state (2 bars).
- Mutations: unguarding `save_state` re-fails the dry-run test; dropping `paired`
  from `symbol_owner` re-fails 3 ownership tests. Both restored.
- `pytest tests/loop` → 363 passed, 3 skipped, 1 failed (the live-lag test).

## Learned

A guard placed on the *path* rather than on the *branch* it is about disables
every decision downstream of it, including the exits. The question "who owns
this position?" is about adoption; it was answered at the top of the loop,
where it also silently meant "do not trade, do not exit, do not advance the
clock". Same shape as the 2026-10-18 lesson: fixing the condition is not the
same as checking every consumer of it.

And: a rehearsal path that writes is not a rehearsal. `last_bar` was protected
and `save_state` was not — the intent was documented in a comment one function
away from the leak.
