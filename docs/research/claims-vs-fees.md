# Re-checking the repo's win-rate claims against fees

Task T1.5 · 2026-09-26 · Repo-only analysis; no production data yet. Numbers marked **derived** are
arithmetic on figures the repo itself states. Nothing here is a new measurement.

## Bottom line

1. **The 152-trade paper ledger that justified the 2026-09-06 "aggressive" shift was, by its own numbers,
   net negative after fees.** The buckets quoted at `scripts/unified_trader.py:67-70` add up to
   **+$0.52 gross** on $5 positions. At 0.144% round-trip cost ($0.0072 per trade), that total stays positive only
   for **fewer than 72 trades** (derived: 0.52 / 0.0072 = 72.2). The comment says the ledger had **152**.
   Unless many of those 152 were outside the three quoted buckets, the ledger lost money once fees are counted.
2. **The studies behind the capitulation and 15m SHARP claims are gross.** `capitulation_catch.py`,
   `parallel_harness.py`, `walk_forward_confirm.py` and `winrate_deep_sweep.py` contain **no fee handling**
   (0 matches for fee/commission/0.00144). `fee_audit.py:5` confirms the pattern: *"Every performance number
   this system produced was GROSS."* So "70.2% WR, +0.44% EV" and "SOL 61% WR with the EMA200 filter" are
   pre-cost, which breaks rule 4 (SYSTEM_MAP §4).
3. **Win rate vs a break-even p\* doesn't apply directly here.** These books don't exit at a fixed TP/SL. They use
   a breakeven lock at +0.5%, a trail from +1.5%, and 4–6h time stops (`unified_trader.py:183-290`,
   `futures_executor.py:36-37,369`). The correct test is **mean net return per trade** from the ledger, which
   needs the production files listed below.

## Claim by claim

| Claim | Where | Fee-adjusted status | What settles it |
|---|---|---|---|
| 5m scalps 69% WR, +$0.37 | `unified_trader.py:68` | net > 0 only if the 5m bucket had < 51 trades at 0.144% RT, or < 37 at 0.2% (**derived**) | count and sum of 5m closes in `data/unified-paper.json` |
| 15m 60% WR, +$0.14 | `unified_trader.py:68` | net > 0 only if < 19 trades (0.144%) / < 14 (0.2%) (**derived**) | same file |
| 30m 52%, +$0.01 | `unified_trader.py:69` | net > 0 only with a single trade (**derived**); in practice negative | same file |
| ELITE 15m SHARP, ETH 87–88%, SOL 80–90%, XRP 68–93% | `unified_trader.py:61-62` | in-sample, 600–800 candles, gross; contradicted by the repo's own 3.6y OOS note | superseded by the row below |
| 15m SHARP + EMA200: OOS +13.5%, SOL WR 51% → 61% | `futures_executor.py:182-184` | gross (the study scripts have no fees); traded at **3x with real money**, where leverage multiplies the fee drag on margin | re-run the study under the T3.3 gate with fees |
| Capitulation catch BTC 30m: 70.2% WR, +0.44% EV, PF 2.92 | `docs/adaptive-framework.md:103` | gross. EV +0.44% per trade is above a 0.144–0.20% round trip, so it **may survive fees** (~+0.24–0.30% net if the EV is accurate) | re-run with fees, shared account, BTC benchmark |
| Regime-adaptive L/S: OOS +40.8%, DD −21.9% | `regime_trader.py` docstring | states "real fees 0.144%", the **only** headline claim that says it includes fees | re-run under the gate to confirm; the best current candidate |

The capitulation net figure is **derived** as EV minus the round-trip cost (0.44 − 0.20 = 0.24; 0.44 − 0.144 ≈ 0.30),
assuming the stated EV is per trade and gross. It isn't a measurement.

## Production files needed (added to E1)

Read-only, from `/opt/trading-agent/data/`:

- `unified-paper.json`: closed paper trades with `tf`, `pnl`, `entry`, `exit`, `reason`, used to compute count, gross sum and net per bucket
- `futures-trades.json`: real futures closes (3x), used to compute net PnL after the actual fees charged
- `exit-params.json`: the current calibrated stop and hold
- `fee-audit.json`: the measured fee rate (already requested)

And one file from outside `data/`: whatever produced `/tmp/live_fine.json` (the 3.6y 15m/30m BTC dataset the
study scripts read). It isn't in git, so the studies can't be reproduced as-is.

## Consequences for the plan

- **T3.3's gate is the fix, not a nice-to-have.** Every inherited claim is re-scored net of fees before it becomes a baseline.
- **Order of re-testing:** (1) the 15m SHARP + EMA200 futures signal, because real 3x money depends on it; (2) regime-adaptive
  L/S, the most promising; (3) capitulation catch, which may survive; (4) the unified_trader 5m/15m buckets, likely negative.
- This finding supports E5 (retire `live_executor`) and raises a question for the owner about `futures_executor` (E7).
