# The week-over-week "improvement" is one evening — and it is not a strategy change

Overnight shift, 2026-10-23. Written after the lesson loop reported no new
trades, so the shift went looking for whether the standing measurements still
say what they said last week.

## Finding

The unified paper book, restricted to the honest post-cost-fix cohort
(`pnl_net` present AND `cost > 0.0015`, i.e. written after the measured-cost
change of 2026-10-10), n=358:

```text
whole cohort        n=358   net +2.1910   mean +0.00612   t=1.31   CI [-0.00306, +0.01530]
split at median     H1 -1.4519 (-0.00820/trade)   H2 +3.6429 (+0.02013/trade)
                    difference 0.02815  SE 0.00927  t=3.04  permutation p=0.0024
```

The half-split is **significant** and the whole cohort is **not**. That is the
trap, and it is the reason this document exists: a split chosen after seeing
the data will always find the crossing point that maximises the contrast.

## The test that settles it

```text
drop 2026-10-18     n=310   net -3.6562   mean -0.01179
2026-10-18 alone    n= 48   net +5.8472
TP exits, whole     28
TP exits on 10-18   20   at 19:46, 20:16, 20:46
```

**One evening contributes +5.85 against a +2.19 total.** Remove it and the book
that looked like it had turned positive is at -3.66.

It is also a market move, not a strategy change. Candles from `trading.db`,
19:00-21:00 on 10-18:

```text
ADA  5m low->high +1.84%    AVAX 5m +2.27%
DOGE 5m +1.83%              LINK 5m +1.79%
ETH  15m 18:00-22:00 low->high +2.37%
BTC  15m 18:00-22:00 low->high +0.92%
```

20 TP exits across 12 symbols inside one hour is a **breadth flush**, the
inverse of the clustered-SL pattern already documented in
`trade-lesson-loop/references/loss-attribution-time-clustering.md`. Clustered
TPs are the market too, and reading them as an edge is the same mistake with
the sign flipped.

## What is NOT claimed

- Not that the book is negative. The whole-cohort CI spans zero; the honest
  statement is **the book has no measurable per-trade edge at n=358**, and the
  week-over-week story is a single day.
- Not that 10-18 should be excluded as an outlier. It is a real trading day with
  real fills. It is reported because the *inference* "the book improved" does
  not survive its removal — not because the trades were faulty.
- Not that any live path should change. No real-money path was touched.

## The generalisable rule

> A time-ordered half-split measures the WINDOW, not the strategy, unless you
> also run a leave-one-day-out check. Report both, and quote the worse one.
>
> Corollary: a clustered **TP** flush is the market exactly as much as a
> clustered **SL** flush. The loss-attribution discipline is sign-agnostic.

On this stack: any "week N improved" claim must carry
`net ex-best-day` beside `net`, the same way a win rate must carry its n.
