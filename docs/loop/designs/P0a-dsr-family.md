# P0a · Pre-registration: family-based trial variance in the daily DSR

Status: **pre-registered 2026-09-27, before any P1 (ROADMAP v3) result exists.** Owner chose option 2.
Applies to every gate verdict from P1 on. It may only be changed by a new pre-registration made before
the results it would apply to.

## Problem
The daily DSR benchmark is `SR0 = sd(trial Sharpes) × z(N)`. On the L2a batch (28 trials), the
intraday trials that lost ~99% have daily Sharpes down to −0.19. They widen the spread so much that
SR0 = 3.4/yr, a bar no strategy of any family can reach. An unrelated failed family sets the bar.

## Rule
- **N = every distinct trial** (unchanged). Every try still counts.
- **The spread `sd` comes from the trials in the tested strategy's own family.** If the family has fewer than 2
  trials, it comes from all trials (the stricter choice).
- **Families are fixed by strategy type in `gate.FAMILIES`,** never by results. A strategy that isn't listed is a
  family of its own. New candidates are assigned a family on their candidate card before they are run.

| family | members |
|---|---|
| trend | trend_sma, trend_real, donchian_ensemble, tsmom_risk_managed |
| cross_section | ctrend_lite, xs_momentum, high_52w |
| intraday | intraday_momentum, intraday_reversal, btc_lead_lag |
| breakout | range_breakout |
| regime | regime_ls |
| benchmark | buy_hold_btc, buy_hold_ew |

Reference: López de Prado (2019), "A Data Science Solution to the Multiple-Testing Crisis in Financial
Research": cluster trials and deflate within clusters. We keep N at the full trial count, which is
more conservative than N = number of clusters.

## Disclosure: effect on the finished L2a batch (not a new verdict)
Trend bar: 3.4/yr → 0.27/yr. Best trend_sma: SR 0.81/yr, DSR 0.868 < 0.95 → **still FAIL**.
BTC buy-and-hold over the same OOS window: SR 0.42/yr.
