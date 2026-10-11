# The paper book charged a stale cost — the measured fee is 5.4x higher

**Date:** 2026-10-11
**Class:** stale-assumption / silent-optimism defect (same family as the stale
edge feed and the missing regime tag already fixed on 2026-10-10)

## What was wrong

`scripts/unified_trader.py` booked every paper round trip at

```
PAPER_COST_RT = 0.0003      # 0.03% round trip
```

a HARDCODED constant set on 2026-10-05 from "30 real fills" whose recovered
fees were ~zero. The accompanying comment called it *"deliberately
conservative … the LOW end, so the paper book is flattered rather than
punished."*

That framing stopped being true the moment the exchange's fees were actually
measured. `scripts/fee_audit.py` (cron, 09:50 daily) now reports, in
`data/fee-audit.json` (as of 2026-10-10T09:50Z):

```
measured_round_trip_pct = 0.16092      # 0.16092% round trip
```

That is **5.4x** the charged rate. So the model was not conservative — it was
OPTIMISTIC by ~0.13% of notional per trade.

## Why it matters

The paper book is the system's active learning ledger; every edge
promote/demote verdict is computed from it. A cost that understates reality by
5x makes a losing strategy look profitable.

Impact on the live book (2026-10-11):

| basis                    | net P&L |
|--------------------------|---------|
| gross                    | +$19.32 |
| net @ old 0.03%          | +$15.09 |
| net @ measured 0.16092%  | **−$3.35** |

Last 72h: gross −$1.87 → **−$3.78** at measured cost.

So the headline "+$19 paper profit" flips to a **loss** once the book pays the
fees the exchange actually charges. This is the same conclusion the walk-forward
replay already reached ("the sharp-dip entry is negative-EV after fees"), which
the inflated paper book was masking.

## The fix

`unified_trader` now resolves `PAPER_COST_RT` by precedence:

1. explicit `PAPER_COST_RT` env var (operator override) — wins;
2. **measured** `measured_round_trip_pct / 100` from `data/fee-audit.json` —
   the default;
3. the old `0.0003` constant — last resort only, when no audit file exists.

`_COST_SOURCE` records which basis was chosen, so the provenance is explicit
rather than implicit. The env var is read at import; cron restarts every 30m,
so the new basis takes effect on the next cycle.

## Guardrail / non-regression

`tests/test_paper_cost_measured.py` (3 tests) pins:
* the measured value is preferred and is NOT the stale 0.0003;
* an explicit env override still wins;
* the percent→fraction conversion is correct.

Paper-only. No order path touched. `pnl` (gross) is unchanged, so every
existing consumer, threshold and comparison keeps working.

## Scope note

This is a *disclosure/measurement* correction, not an alpha change. It does not
make the strategy profitable; it makes the ledger honest. The open question it
sharpens — the entry signal is negative-EV at real cost — remains the top
research item.
