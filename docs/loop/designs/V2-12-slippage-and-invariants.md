# V2-12: gap/slippage protection and whole-chain invariants

## How it was found
The per-component tests all passed. A randomised whole-chain test (60 seeds: random Jev replies incl. hostile and failing
ones, random kill switch / pauses / exchange health, random price shocks) initially exercised almost no trading
(4 would-executes, 3 positions, 0 stop-outs), so it was passing mostly vacuously. After tuning it to trade (and adding a
coverage floor so it cannot go vacuous again) seed 25 failed: **the risk engine approved an order the account could not
fund.** Cause: risk sizes at the REFERENCE price (last close) but the order fills at the next bar's open. A 6% gap up made the
fill cost more than the cash, and, worse, with the stop fixed at 2% below the reference, the real risk per trade becomes
~8% / 2% = 4x the 2% the engine meant to allow.

## Choice (from the owner's note, which `contracts.md` had dropped)
The note's Risk Decision carries `max_slippage_bps`. Added: `RiskDecision.max_slippage_bps`; `ExecutionCommand.worst_price`
(buy: fill must be <= it; sell: >= it), required to open and forbidden on reduce-only orders (an exit must never be refused
for price); `from_approved` derives it from the proposal's reference price; `PaperBook` refuses an entry that would fill
beyond it (`slippage_exceeded`), never fills at the worse price.

## Second-order bug found while proving it
At exactly the worst price the fee and the slippage COMPOUND: (1.001)(1.001) = 1.002001 > the 1.002 buffer I had set, so the
order was short by ~$0.001. The relationship is now enforced, not commented: `RiskLimits.__post_init__` refuses a
`cost_buffer_frac` below `(1+fee)(1+slippage)-1` (default 0.0025), and a 400-case random test checks fundability.

## Failure modes / self-review
1. Defaults are UNVALIDATED: fee 0.10% (repo CostModel; real rate UNKNOWN), slippage 10 bps (note shows ~8), buffer 0.25%.
2. A refused entry is a missed trade by design; the count is visible in the log (`rejected:slippage_exceeded`).
3. Real-exchange slippage on a market order is not bounded by us; live needs a limit/protect order. The worst_price is
   the contract for that; the live execution engine does not exist yet.
4. The invariant test is only as good as its randomness: coverage floors (would_execute, opened, closed, stopped, exits,
   six distinct risk vetoes, fallback, kill, pause, slippage refusal) fail the suite if the runs stop reaching those paths.
