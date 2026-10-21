# A per-edge verdict is about an edge that is still trading

**Date:** 2026-10-21
**Status:** implemented (improvement_loop, self_improve, paper_book)
**Tests:** `tests/test_edge_liveness.py` (8)

## What the owner was reading

`reports/improvement-report.json` (09:30Z run) carried these as actionable
suggestions, priority 2, on every run:

```
Edge 'supertrend' DECAYING (net): 31.3% WR / 134 trades / -1.87 net (gross -0.82)
  — net-NEGATIVE despite a 31.3% win rate (wins too small, losses too large).
  Tighten its entry (deeper dip / lower RSI) or stand it down — it is losing edge.
Edge 'bollinger' ... 'ema-cross' ... 'momentum' ... 'pullback' ... 'breakout'
```

## What was actually true

Measured on the collapsed book (`paper_book.collapse_multitf`) that same hour:

| edge | n | net | last fill | days silent |
|---|---|---|---|---|
| pullback | 17 | -1.11 | 2026-09-06 | 46 |
| rsi2 | 7 | -0.24 | 2026-09-08 | 43 |
| supertrend | 134 | -1.87 | 2026-09-08 | 43 |
| bollinger | 47 | -1.04 | 2026-09-10 | 41 |
| ema-cross | 22 | -0.84 | 2026-09-12 | 39 |
| momentum | 27 | -0.17 | 2026-09-13 | 38 |
| breakout | 18 | -0.44 | 2026-09-13 | 38 |

All seven were on `data/signals-state.json → disabled`. None had opened a
position in five to six weeks.

So seven of the seven per-edge verdicts were **unactionable**: "stand it down"
for an edge already off is a no-op dressed as an action, and "tighten its entry"
is advice that no live code path can act on. The defect is not that the numbers
were wrong — they were right about the population — but that a population with
**no time bound** is presented as the current state. The card could not tell a
verdict about a live design from a verdict about a corpse.

This is the same class as SYSTEM_MAP rule #12 (a verdict must state its window)
and rule #9 (never quote a measurement; re-derive at write time), applied to
time instead of to a string.

## The mirror defect

A frozen record with a *good* mean reads as **proven forever**. The promote
gate (`n>=10, wr>=60, net>0`) is likewise unbounded, so an edge that was
switched off at its peak keeps its claim to real money indefinitely. A number
that cannot change is not evidence that anything is still true.

## Fix

`paper_book.edge_liveness(records)` → `{last_fill, redundant, stats}`.

1. **Read side** (`improvement_loop._suggest`): an edge with no fill in
   `LIVE_DAYS=14` draws **no** tighten/stand-down verdict. It is not silently
   dropped: one `edge_not_live` note names each stale edge with n, net and last
   fill date and says the verdict is HISTORY. An edge whose last fill cannot be
   parsed is treated as LIVE — the read side must not silence what it cannot
   date.
2. **Write side** (`self_improve`): promotion is refused for a not-live edge and
   recorded as a `change` with the reason. Here an undateable stamp fails
   CLOSED (not promotable): the write side moves real money, the read side only
   speaks.
3. **Redundancy:** edges sharing another's handoff — same tf (or one missing),
   same regime, *exact* entry price, within 72h — that add nothing (not higher
   net EV, not higher WR) while the incumbent has `n>=30` are REDUNDANT COPIES.
   Measured: `breakout ~ loose`, `momentum ~ loose`, `rsi2 ~ supertrend`. A copy
   draws no independent verdict and cannot be promoted on its parent's evidence;
   one experiment wearing two names is not two confirmations. The three live
   edges are *not* flagged as copies (they differ in timeframe), so the rule does
   not collapse the real book.

## Deliberately NOT done

- No edge was re-enabled. Re-enabling is an owner decision; the note says
  "re-enable ONE at a time and let it fill for 14d".
- No threshold was tuned. The live book's single remaining opener `validated`
  (n=942, 45.0% WR, +1.34 net) is still measured, and `loose` (43.1% WR,
  +3.40 net, last fill 2026-10-11 — 10d, so live) still gets its preventive flag.
- `night_tuner` already scopes per-edge stats to a 72h window, so it does not
  have this defect; the two loops now agree in effect.
