# V2-7: paper book + paper-backed context (V2.1)

Source: owner's note "V2.1 Paper: Jev -> Policy -> Risk -> Paper Execution", `docs/v2/contracts.md` rollout ladder.

## Problem
The shadow pipeline sees no position, so after a would-execute LONG it proposes another LONG every cycle and its
would-execute count overstates trades. It also has no source for equity, day/week start, high-water mark or open
orders, which the risk engine requires.

## Choice
- `PaperBook` fills `ExecutionCommand`s in simulation. It never sizes: size and stop come from the risk decision.
  It reuses the repo's `CostModel` for pricing (fee, half spread, slippage) so costs stay consistent with the replay
  engine. The replay `PaperAccount` was NOT reused because it sizes positions itself (`equity/slots`), which
  contradicts "size is the risk engine's".
- Fills at the first 1m bar open at or after submission (no look-ahead); `idempotency_key` makes a retry a no-op;
  one position per symbol; reduce-only closes at most what is held; protective stop checked on every bar including
  the entry bar, a gap fills at the open; LIMIT orders are rejected, not approximated; SHORT is paper-only.
- Its own hash-chain lane (`v2paper`), `mode: PAPER_V2`, `real: false`. Never mixed with the shadow lane or any real record.
- Day and week start are computed with `timedelta`, not the legacy `_utc_week_start` (E6).
- `PaperContextProvider` builds the pipeline's `CycleContext` from the book. What a book cannot know is injected and
  `None` means unknown, which the risk engine fails closed on (exchange health, kill switch).
- `ShadowPipeline(paper=book)`: bars drive the book (fill, stop, mark) BEFORE the next decision; an approved preview is
  submitted to the book; records are labelled `paper`.

## Found by integrating (not by unit tests)
**Risk v2 approved orders the account could not pay for.** The legacy sizing (2% risk / 2% stop) is 100% of equity,
so notional plus the entry fee exceeded cash and the paper book rejected 3 of 4 approved orders for insufficient cash.
A live spot account would refuse the same order. Fix in the V2 wrapper only: notional is capped at
`free cash / (1 + cost_buffer_frac)` (0.002, UNVALIDATED), a new `buying_power` check records when it bites. It can only
shrink a size. The legacy engine is untouched. This is E7(d) turning real.

## Failure modes / adversarial self-review
1. *Paper flatters reality.* Default costs are 0.10% fee, no spread, no slippage (the repo's own defaults; real costs
   UNKNOWN). Paper P&L is an upper bound by that gap; say so wherever it is shown.
2. *A same-bar stop and entry ordering.* The stop is checked after the entry fill on the same bar, as the replay broker does;
   a bar that both fills and stops us books a loss, which is the conservative reading.
3. *Daily-loss cap blocks re-entry after a stop-out* (observed: a ~$1,010 loss against a $100 cap rejects every later
   cycle). Correct, and tested; the cap value is the owner's (E7a).
4. *`daily_loss_budget_used` has no written definition* (E8): the provider requires an owner-supplied budget, else reports 0.
5. *No funding/borrow cost on shorts, no partial fills, no queue position.* Out of scope for V2.1; shorts stay disabled
   by default in risk.
6. *A conservation test cannot see mis-attribution* (a mutation that charged the whole entry fee on a partial close
   survived until a per-trade assertion was added).
