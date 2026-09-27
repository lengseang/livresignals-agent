# P0b · Pre-registration: every strategy must beat two benchmarks

Status: **pre-registered 2026-09-27, before any P1 result.** Applies from P1 on.

## Rule
A PASS additionally requires the strategy's **OOS daily net Sharpe** to be **strictly greater** than, over
the same OOS days and with the same cost model:
1. **BTC buy-and-hold** (`bh_net`, existing): BTC bought at its first tradable open.
2. **Equal-weight basket** (`bh_ew_net`, new): every coin in the run's universe bought once at its first
   tradable open with **1/N of the starting equity**, never rebalanced; a coin not yet listed keeps its
   share in cash.

**Amendment 2026-09-27, before any P1 result:** the first cut used min(cash, equity/N). Early coins rose,
so later purchases took bigger slices and cash ran out: on the full 2017-2026 history the basket never bought
DOT, AVAX, NEAR, ARB or SUI (found by the P1c identity re-run). Fixed-share buying repairs it.

Missing curve (runs made before P0b) → "not computable" → INSUFFICIENT, never a guessed pass.
Sharpe is compared, not return: trend strategies trade return for smaller drawdowns, and the independent
evidence (summitward 2017-2025 test) says that is their real benefit. Returns and drawdowns of all three
are still reported.

## Smoke check (real data, 2023-10 .. 2024-06, BTC/ETH/SOL, OOS from 2024-01)
trend_sma OOS Sharpe 1.01/yr vs BTC hold 2.07, basket 1.78 → FAIL. Basket return +315.6% (SOL ran).

## P0d amendment (owner approved 2026-09-27, before the next batch)
Target-weight runs (a strategy_signal with action "target" or a rebalance event) are judged on **OOS weeks with at
least one fill >= 30** instead of 30 closed OOS trades, and their per-trade economics are reported as
not applicable (positions are trimmed, not round-tripped). Everything else (OOS net return, daily DSR,
benchmarks) is unchanged. On P1 this changes no verdict: A1 now fails only on DSR (217 active weeks).
