# Regime test: the one real edge, and what it is actually worth

**Date:** 2026-10-11 · **Status:** finding recorded — real, positive, too small to deploy

## The question

Every rule tested this week lost to buy-and-hold. The structural reason: these
assets rose for years, and any rule that leaves the market to time it gives up more
than it gains. The untested idea was whether the same rules have edge where there is
no free trend to lose to. We have no non-crypto data (the DB holds 10 crypto majors),
so the test used non-overlapping 120-day windows classified by the basket's own
return: UP (>+10%), FLAT (±10%), DOWN (<−10%).

## What the regime split revealed

```
                     UP windows              DOWN windows
dip-long        5 win  PF 0.91  0/5 beat   9 win  PF 0.72   1/9 beat
breakout-long   5 win  PF 1.59  1/5 beat   9 win  PF 0.61   5/9 beat
bearcross-short 5 win  PF 0.52  0/5 beat   9 win  PF 1.85   9/9 beat   <-- ALL NINE
```

The bear-cross short beats the basket in every down-window and loses in every
up-window. That is the signature of a directional edge, not beta — and it explains
why the earlier full-period tests showed nothing: they averaged the two regimes and
the up-period losses cancelled the down-period wins.

## The stress test, and what it changed

9/9 can be an artifact (one huge window). It is not: per-window breakdown of the
short —

```
7 of 9 down-windows positive
largest single window = 38% of the total
no window carries the result
```

Distributed, not concentrated. The edge survives the test that killed the
day's other candidates.

## But the honest number is the full-period one

A continuous run over 2022-2026, which is what a live system would actually trade:

```
                        n     PF     win    net on a $5 book
stand-alone short      285   1.20    29%    +$8.25
regime-gated short     186   1.26    31%    +$6.81
```

**The regime gate does NOT help.** It lifts PF 1.20 -> 1.26 but cuts the total by
removing trades. It is a wash, and the always-on version makes more money.

So the truthful summary is: **the bear-cross short is the only rule this week that
is positive over the full period — PF 1.20, n=285 — and the 1.85 figure is the
down-window average, a different and more flattering statistic.**

## Verdict

```
REAL      a positive short-side edge exists. PF 1.20 full-period,
          1.85 averaged over down-regimes, distributed across windows.
HONEST    PF 1.20 does not beat holding. On $23 at ~1.5%/trade and ~20
          trades/year it is worth a few dollars a year.
DECISION  NOT deployed. A PF 1.20 rule does not justify real capital or the
          operational risk of running a short against a rising market.
```

## The lesson

Regime matters more than parameters. The same rule is PF 0.52 or PF 1.85 depending
on the regime, and a test that mixes regimes reports an average that describes
neither. Every hunt run before this one made that error, including the day's own.
**Split by regime first; then judge.** That is now recorded as the method.

The bear-cross short is kept as a known-positive component for a future system that
can also be long when the regime is up — the two together are the only combination
this week that is not strictly worse than holding.
