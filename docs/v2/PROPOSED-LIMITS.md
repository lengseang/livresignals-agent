# Proposed V2 limits (E7, E10): derived from measured market data, awaiting your approval

**Status: PROPOSALS, not approvals.** Computed by `python -m trading.v2.calibrate` from ~18,000 real one-minute bars (6 pages x 3 symbols)
plus 500 hourly bars and 15 live order-book samples per symbol, on 2026-10-04 (BTCUSDT, ETHUSDT, SOLUSDT). Re-run it any time; every
number below is a rule applied to data, so you can accept the number, change the rule, or override it. The runner uses them via
`python -m trading.v2.calibrate --run` (PAPER ONLY).

| Setting | Proposed | Rule | Evidence from the sample |
|---|---|---|---|
| Kill: max one-step price move (feed-corruption trip) | **3.0%** | 3 x the largest 1-minute move, clamped 2%..15%, rounded up to 0.5% | largest 1-min move 0.84%; 99.9th percentile 0.42% |
| Paper spread cost | **0.82 bps** | worst per-symbol median book spread | BTC 0.001, ETH 0.037, SOL 0.82 bps |
| Paper slippage | **1 bp** | an assumption (no fills to measure) | none: UNKNOWN until real fills exist |
| Risk: max entry spread | **5 bps** | max(5, 4 x worst spread) | the spec's example cap is already 5x the worst spread seen |
| Stop distance | **3.5%** | 3 x the 90th-percentile hourly ATR(14)/close of the widest symbol, clamped 1%..5% (5% = legacy max stop) | p90 hourly ATR: BTC 0.70%, ETH 0.92%, SOL 1.14% |
| Target distance | **7%** | 2 x the stop (the spec's 1:2) | none: a convention |
| Daily loss cap (risk engine) | **$20 = 2% of $1,000** | 2% of paper equity; tighter than the legacy 5% halt | one stop-out at the legacy 2%-risk sizing is ~$20, so the cap allows about one stop-out a day |
| Kill: daily loss cap | **$40 = 4%** | 2 x the risk cap (the emergency stop) | |
| Daily-loss budget (legacy "2x used" breaker) | **$20** | equity x the legacy 2% risk per trade = one full stop-out, so the breaker means two stop-outs a day | the legacy definition is not written down (E8) |
| Kill: exchange failures to engage | 3 | consecutive bad health readings | judgement |
| Kill: max feed age | 180 s | 3 x the polling interval | judgement |
| Kill: max orders per minute | max(5, 2 x symbols) | | judgement |
| Hermes halt request: min confidence | 0.8 | | judgement |
| Fee | 0.10% | the repo's CostModel (the account's real rate is UNKNOWN) | UNKNOWN |

**What I am NOT claiming:** that any of these maximise anything. They are conservative, data-anchored starting points for a PAPER run; the
only evidence that matters is what shadow/paper results show over weeks. The 3.5% stop sizes a position to about 57% of equity at the
legacy 2% risk per trade (2% / 3.5%): smaller than the 100% the spec's 2% stop implied, which is itself a finding in the owner's favour.
Volatility changes: re-calibrate regularly rather than treating these as constants.

## Live evidence with these numbers (2026-10-04, 5.5 minutes, 3 symbols, 2-minute cadence, real Jev)
9 decisions, 0 fallbacks, 0 feed failures, kill switch never engaged. Jev said NO_TRADE for all three at 0.80-0.97: no paper trade was
opened, so the entry path was NOT exercised live (it is covered by the unit and invariant tests). Two live bugs were found and fixed on
the way (a book stamped after "now"; a single "now" for a slow multi-symbol step).

## How to approve
Reply with: "approve all", or edit the table (e.g. "stop 2.5%, daily cap $30"). I will put the numbers in a named preset and keep the
rule beside it. Real-money use is a separate decision and is not proposed here.
