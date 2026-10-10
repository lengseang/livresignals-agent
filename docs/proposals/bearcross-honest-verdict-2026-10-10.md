# The bear-cross short, re-tested honestly: it does not survive

**Date:** 2026-10-10 · **Status:** verdict — NOT deployable

## What this corrects

Earlier on 2026-10-10 the short-side hunt reported the bear cross 50/200 as the
one working family: **PF 1.30-1.68 in both halves, ~+40% in both halves**, and it
was the only positive, stable rule found all day. On that basis deployment to the
$23 futures wallet was considered.

Then the IS window was checked against the data, and it did not hold up:

```
BTC/USDT  starts 2022-01     ETH/USDT  starts 2022-01
SOL/USDT  starts 2022-01     ARB/USDT  starts 2023-03
XRP 2018-05  BNB 2017-11  ADA 2018-04  LINK 2019-01  DOGE 2019-07  AVAX 2020-09
```

The "IS 2017-01-01 .. 2023-01-01" window the engine was run with **did not exist
for a third of the universe.** The engine silently used whatever bars were present,
so "2017-2023" was really "2022-2023" for BTC/ETH/SOL and 2023-only for ARB. Every
IS number from the day inherited that error.

## The honest re-test

`bearcross_honest.py`:

- IS = 2022-01-01 .. 2024-07-01 — a window every symbol actually has
- OOS = 2024-07-01 .. 2026-10-01 — after the IS, untouched
- ARB excluded entirely (no history before 2023-03) rather than contributing a
  partial window
- funding charged, adverse-first exits, next-bar fills, costs on

Benchmark over the honest window: **hold IS −13%, OOS +12%** — a *flat* market, not
the bull run the earlier numbers rode.

```
stop  tgt  hold | PFis  PFoos   netIS   netOOS
  4%  12%  540  | 1.39   0.92   +6.67   −2.79     OOS loses
  6%  18%  540  | 0.98   1.24   −3.40   +4.98     IS loses
  8%  24%  540  | 0.98   1.28   −4.26   +7.10     IS loses
  6%  12%  180  | 1.44   1.10   +9.21   +0.93     only survivor
  4%   8%  180  | 1.37   0.87   +5.73   −3.48     OOS loses
```

## Verdict

**Four of five configs fail outright. The one that passes returns +$0.93 over 134
out-of-sample trades — about +$0.007 a trade, indistinguishable from zero.** It
clears the bar only because the bar is "positive", not because the edge is real.
The per-symbol spread is even (top symbol 14%), so it is not one coin carrying it —
it is simply nothing.

PF flipping sign between IS and OOS as the exit changes is the signature of noise,
not edge. The bear-cross signal does NOT robustly survive an honest window.

**NOT DEPLOYED. The $23 futures wallet is untouched.**

## The lesson, which is worth more than the strategy

The one guard that caught this was checking the window against the actual data —
something no test in the day's sequence did automatically. The engine will happily
run on a window that is 80% empty for a symbol and report a number as if the
window were full. **Any future hunt must assert, per symbol, that the requested
window is actually covered by bars, and refuse (or say so loudly) when it is not.**

Filed as the next engine hardening task.
