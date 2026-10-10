# Short-side hunt: the falsification plan, written BEFORE the run

**Date:** 2026-10-10 · **Status:** pre-registered, then run

## Why this hunt exists and why it is different

Every search so far has been long-only, and every one lost to buy-and-hold:

```
hunt 1   high-freq dip          fee-bound, empty
hunt 2   low-freq breakout      PF 1.29 real, but loses to holding (+212% vs +228%)
futures  breakout on 1d         same: loses to holding (+437% vs +2,117% IS)
```

The structural reason (recorded 2026-10-10): on assets that trend up over years,
**any long-only timing rule loses to holding by construction** — it pays fees to
leave and re-enter a trend it would have kept by sitting still.

Shorts are the first search that does NOT compete with buy-and-hold. A short
profits when price falls. Buy-and-hold cannot capture that leg at all. If there is
an edge in the market's asymmetry, this is the only place it could be that holding
cannot already beat.

## What the data says could justify it (evidence, not hope)

```
BTC 15m, 2025-01 to 2026-10
  up-runs    max 11 bars, mean 1.92
  down-runs  max 16 bars, mean 1.93     <- drops cluster LONGER
  max drawdown -53.8% in a year ending -12.2%
  sum up-moves +4737%  vs  sum down-moves -4733%   <- equal in TOTAL
```

Counts are symmetric; the SHAPE is not. Longer down-runs, deep drawdowns. That is
the mechanism a short could exploit — if it is real and not an artifact of one
period.

## Falsification criteria (written before running — these decide the verdict)

**H1 — the short leg is not symmetric noise.**
A short rule entered on RALLIES (the mirror of the dip) must beat PF 1.0 in BOTH
halves. If it does not, shorting is no better than longing and the asymmetry
finding is not tradeable.

**H2 — it must beat the SHORT BENCHMARK.**
The mirror of buy-and-hold is short-and-hold. If a short rule underperforms simply
shorting and holding, it is timing a leg it could not beat by not moving — the
same failure as the longs, and it is rejected.

**H3 — funding must not eat it.**
A short pays funding every 8h. Histories show crypto funding is usually POSITIVE
(longs pay shorts), which is a tailwind for shorts — but it is not always, and the
model must charge it. If a candidate only survives with funding at zero, it is
rejected. The hunt charges funding explicitly at a conservative rate.

**H4 — enough trades to mean something.**
Fewer than 100 OOS trades is noise, whichever way PF points.

## The honest prior

Most likely outcome, stated in advance: **nothing survives H2 either.** The
structural argument cuts both ways — in a market that rises net, shorts fight the
drift, and the drift is a powerful tailwind for longs and a headwind for shorts.
But the asymmetric SHAPE (long down-runs) is the reason to look rather than assume.
Either answer is a real result.

## Method

- Engine: `replay_engine.py`, now short-capable (verified: inverted PnL, inverted
  stop/target, long path unchanged at 12/12 self-test).
- Timeframes: 4h and 1d.
- Entries: mirror of the long families — sell rallies (above N-bar low), bearish
  trend cross, breakdown (below N-bar low).
- Exits: wide, mirroring the long grid.
- Costs: 0.05%/side (futures taker) + funding charged per 8h held.
- Gate: PF > 1.0 in BOTH halves AND beat short-and-hold AND >= 100 OOS trades.
