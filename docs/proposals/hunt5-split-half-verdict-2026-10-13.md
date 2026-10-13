# hunt5: the standing hunt's findings, split-half tested — 2026-10-13

**Author:** overnight improvement shift · **Status:** evidence, not a deployment
**Scope:** the 65 findings written by `scripts/hunt5_open_ended.py` at 2026-10-13
01:50–02:11 UTC. Every number below is **backtest**, 4h bars, 9-symbol basket,
net of 0.05%/side fee + 0.05%/side slippage.

---

## The problem this file answers

`hunt5` ran one pass (`--top 20`) and recorded **65 findings from ~240 configs — a
27% pass rate.** The same research discipline yields ~1% elsewhere
(`strategy-generation-loop`: 52 survivors from 4,860 attempts). A gate that passes
27% is a filter, not a gate. So the findings were re-tested under the *strategy-book*
gate the single-regime hunt does not apply:

- **H1 — split-half (the one that matters).** Net-positive **and** PF > 1.0 in
  BOTH independent halves. A candidate positive in one half only is a regime
  artefact and is rejected regardless of the total.
- **H2 — benchmark.** Must beat equal-weight buy & hold of the same basket.

## What hunt5's own gate misses (why the 27%)

hunt5 enforces: down-regime `n >= 200`, down-regime `PF > 1.0`, down-regime beat
the basket `>= 70%` of windows, and `up_net > -25` (an **absolute dollar floor**
on a $5-notional 120-day window, which almost everything clears). It never tests
both halves. Three of its four best-ranked candidates fail the split test.

## The split-half result (the decisive table)

```
mechanism            half                     n     net    pf   basket
volbreak 20 x2.0     IS  2022-01..2024-05     542  +13.09  1.17  -17.6%
volbreak 20 x2.0     OOS 2024-05..2026-10     594  +12.69  1.15  +14.3%
volbreak 100 x1.5    IS                         402  +13.37  1.24  -17.6%
volbreak 100 x1.5    OOS                        438   +9.10  1.14  +14.3%
volbreak 55 x3.0     IS                         257   +0.52  1.02  -17.6%   <- dead
volbreak 55 x3.0     OOS                        333  +14.21  1.41  +14.3%
bearcross 20/100     IS                         259  +13.10  1.32  -17.6%
bearcross 20/100     OOS                        293   +1.44  1.03  +14.3%   <- decayed
```

Config: `volbreak 20 x2.0` = short when close < 20-bar low AND volume > 2× the
20-bar average; stop 4%, target 12%, hold 120 bars.

**Verdict:**
- **PASS — `volbreak 20 x2.0`** (balanced n 542/594; the only mechanism PF > 1.1 in
  both halves). Second: **`volbreak 100 x1.5`**.
- **REJECT — `volbreak 55 x3.0`**: hunt5's #1 by PF. IS net +$0.52, PF 1.02 =
  flat. Its 1.41 is an **OOS-only artefact**.
- **REJECT as decaying — `bearcross 20/100`**: hunt5 recorded PF 1.88 on one
  window; OOS collapses to +$1.44 / PF 1.03.

## Symbol concentration (volbreak 20 x2.0, full period)

```
DOGE +7.68  LINK +5.66  ADA +5.15  SOL +4.50  XRP +3.08
AVAX +1.68  ETH -0.04   BTC -0.15  BNB -0.98
TOTAL n=1136  net +26.58   positive 6/9 symbols
```

No single symbol dominates (top = 29%). The majors (BTC/ETH/BNB) are flat; the
edge lives in the higher-beta alts. A coherent mechanism, not one symbol's luck.

## Honest limits — what this is NOT

- **Short-only.** It needs futures. It is **not** a competitor to buy & hold and
  must never be presented as one.
- **Modest.** PF ~1.15–1.24. Same order as the accepted `volbreak 100 x2.0`
  (PF 1.23) already in `leng-trading-stack` — this **corroborates** that family
  on a second parameterisation, it does not add a new one.
- **Backtest.** No live-forward record. Not publishable, not deployable.
- **Multiple-testing not corrected.** 65 findings from one rotating sweep. The
  survivors here are declared *before* a deflated-Sharpe correction; treat PF as
  optimistic by an unknown margin. The two the split test keeps are the two that
  must survive that correction next.

## Recommended next step (owner-gated)

1. Fix `hunt5` itself: add the split-half gate (reject one-half-only). This is the
   one concrete change — it is why 27% passed. **Do not loosen anything else.**
2. Run the two survivors through the deflated-Sharpe / multiple-testing gate
   (T3.3) before any paper or futures consideration.
3. Until then: **no change to any live path.** hunt5 writes findings only; nothing
   it produced is wired to an order.
