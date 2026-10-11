# Rotation/momentum strategy, audited: consistent, but loses to buy & hold

**Date:** 2026-10-11 · **Status:** verdict — not deployable, recorded

## Why this was looked at

The overnight `night_research.py` run reported a momentum rotation strategy —
top-N by momentum, 21-day hold, 1% risk, SMA regime filter — as the most
promising thing the loop had produced: **+21% median, positive in 4/4 folds**,
against a basket whose median was −50.7%. On that reading it looked like the
first real edge found all week.

## What the audit found

The result is real as far as it goes. Read from the run's own JSON:

```
candidate           median   worst   folds    per_fold
live (sma_r=150)    +19.51%  +6.77%  4/4      [6.77, 33.89, 18.29, 20.72]

controls
  BTC buy & hold    +27.79%           3/4      <- THE BAR
  shuffled returns  −45.43%
```

`night_research.py` is better built than the day's hand-rolled hunts: NON-overlapping
folds, a consumed-validation gate (a fold with no trades is a FAILURE, not a pass),
drawdown ranked rather than merely reported, and two explicit refutation controls —
buy-and-hold and shuffled returns. Its own docstring cites two prior incidents where
a gate passed and the claim was still wrong.

## Verdict

**The strategy passes consistency and fails the benchmark.**

```
✅ positive in 4/4 non-overlapping folds
✅ worst fold +6.77% — no fold loses money
✅ beats the shuffled control (−45%) by a wide margin
❌ LOSES to simply holding BTC (+19.5% vs +27.8%)
```

A long-only rotation that stays mostly invested will roughly track the market, then
pay fees and miss some upside doing so. That is exactly what the numbers show. It is
not an edge; it is a slightly worse way to be long.

**NOT DEPLOYED. `applied: false` in the run itself — the script only measures.**

## The confirmation, now six times over

```
timing one asset (dip, breakout)     loses to holding
rotation across assets               loses to holding
shorting the down-legs               loses to holding
───────────────────────────────────────────────────
```

Every rule tested this week — mine and the loop's — loses to holding the assets. The
market's rise IS the return; any rule that leaves the market to "time" it gives up
more than it gains. The loop's own controls prove it with numbers no one fed it.

## What this leaves

Two honest paths remain, and neither is a tweak:

1. **A market that is not in a multi-year uptrend** — where timing must add value
   because there is no free trend to lose to. Untested.
2. **Accept it and hold.** Today, again, doing nothing beat every rule tested.

The first is worth one real test. The second is what the evidence currently supports.
Neither is a reason to trade the $23 on a strategy that underperforms sitting still.
