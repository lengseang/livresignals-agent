# Futures signal hunt: what the numbers say

**Date:** 2026-10-10 · **Status:** verdict recorded, nothing deployed

## Why this was run

Owner: "use real money to trade good signals with trading and future (future is
preferred)", reasoning that the spot path has a fee problem and futures might not.
Instruction: hunt the futures symbols and deploy if a signal qualifies.

## The fee premise was checked, not assumed

```
spot      0.10%/side taker  ->  0.30% RT of capital
futures   0.05%/side taker  ->  0.10% RT of NOTIONAL
          at 3x leverage    ->  0.30% RT of MARGIN
```

Futures' headline fee is half of spot's, but leverage multiplies it back: at 3x a
round trip costs the same 0.30% of the owner's actual money. **Futures does not
fix a fee-bound edge. Lower frequency does** — a trade whose expected move is
large relative to 0.30% survives; 20k trades/year cannot.

## Wallet reality (updated 2026-10-10)

```
futures USDT    $23.02   (separate account from the $20.12 spot wallet)
configured max notional $18  ->  needs $6.00 margin at 3x
```

Earlier the wallet held $2.91, less than the $6 margin a full-size trade needs —
config and balance disagreed and any full-size attempt would have failed on
margin. The owner has since funded it to $23.02, so the configured size is now
fundable. Recorded because the mismatch was real and only resolved by funding.

## The hunt

`hunt2_lowfreq.py` on 1d, futures cost 0.05%/side, gate: reject anything above
500 trades/year (that is the class already ruled out).

```
205 of 432 configs "survived" (PF > 1 in both halves)
```

A ~47% pass rate is not discovery — it is a bull market. Same artifact as the 4h
run (378/432).

## The control that decides it

`beat_benchmark.py`: does a candidate beat equal-weight BUY AND HOLD of the same
symbols over the same window?

```
IS  2017-2023   buy&hold +2,117%   best breakout +437%   LOSES
OOS 2023-2026   buy&hold   +228%   best breakout +212%   LOSES
```

**All 16 tested configs lose to holding, in both halves.**

The breakout family's PF (1.26–1.78) is REAL — consistent across both halves. But
it earns LESS than sitting still:

```
buy & hold   captures the whole trend, no stops, no fees
breakout     stops out inside the trend, pays fees, misses the continuation
```

A positive PF is necessary and nowhere near sufficient. The benchmark is the test.

## The structural finding (the useful part)

On assets that trend upward over years, **any long-only timing rule loses to
holding by construction**: it pays fees to leave and re-enter a trend it would have
captured by never moving. This is not a tuning problem — it bounds the approach.

Ways that could actually beat it, none yet tested:

1. **Trade BOTH directions** — short the down-moves. A real edge only if it
   exists. The down-runs cluster longer (max 16 bars vs 11 up) which is suggestive,
   not proof.
2. **Trade a universe where holding is NOT a free +200%** — a basket that is flat
   or falling, where timing must add value because there is no free trend to lose to.
3. **Accept the result and hold the assets.** Today, doing nothing beat every rule
   tested. Worth stating plainly even though it is not a product.

## Decision

**NOTHING DEPLOYED.** No futures entry was re-enabled. The old sharp-dip futures
signal stays paused (four OOS tests, negative-EV — see `futures_executor.py:57-83`).
`config/LIVE_ENABLED` left exactly as found. This document changes no live state.

The owner has authorised deploying to futures the moment a signal passes the gate.
No signal has passed.
