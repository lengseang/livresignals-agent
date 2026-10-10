# Volume-spike confirmation does not rescue the entry — a NEGATIVE result

**Status: TESTED + RECORDED — 2026-10-10. Code added, hypothesis rejected. No
live/paper book touched.** Runnable check: `scripts/hunt.py --only-fam dip_vol,brk_vol`.
Result artifact: `reports/hunt/2026-10-10T011755Z_15m.json`.

---

## 1. The suggestion this closes

`scripts/improvement_loop.py` ranks, as a standing priority-4 `explore` idea:

> "add a volume-spike confirmation (capitulation often comes on high volume, not
> just high vol) — test vol AND volume z-score > 2 as an extra gate."

It had never been tested. This iteration made it testable in the system's own
disciplined search (`hunt.py`) and ran it.

## 2. What was built (code)

Two declared entry families added to `hunt.py` (no evolution, no fitted curve):

- `make_dip_vol`  = `make_dip` + a volume z-score gate
- `make_brk_vol`  = `make_breakout` + a volume z-score gate

`_vol_z(bars, win)` is the population z-score of the newest CLOSED bar's volume
against the prior `win` bars; the gate requires `z >= vz_min`. A zero-variance
window returns `None` and the gate refuses (it can never invent a signal). Volume
is read from the same immutable `LockedFeed.Bar` the engine fills from, so it
cannot peek. Small fixed grid: `vz_win in {24, 96}`, `vz_min in {1.0, 2.0}` →
16 `dip_vol` + 8 `brk_vol` = 24 candidates. Unit tests:
`tests/test_hunt_volume_gate.py` (6 tests: z math, strict-superset property,
degenerate-window refusal, declared-list counts).

## 3. The result

Ran the 3-stage funnel (screen 2025 → full IS 2017–2023 → OOS 2023–2026, next-bar
fills, exchange taker 0.10% + 0.05% slip per side, always charged):

```
FUNNEL  stage1 screen 24x6 on 2025-01-01..2026-01-01
    ...10/24 entries, 0 passed screen
    ...20/24 entries, 0 passed screen
  stage1: 0 of 24 entries beat PF 1.0 in 2025  (818s)
  VERDICT: nothing survives the screen. The space is empty.
```

**0 of 24 volume-gated candidates cleared PF 1.0 even in the screen window.**
None reached IS, so none reached OOS. Same empty space the base dip/trend/breakout
families already reported (`reports/hunt/2026-10-07T03-25-37Z_15m.json`).

## 4. What this does and does NOT claim

- **Does:** a volume-spike confirmation of this form adds no tradeable edge at
  exchange cost on 15m, over the entry rules already tested. The suggestion is
  now a *tested negative*, not an open idea.
- **Does NOT:** prove every volume filter fails. The grid is small (2 windows ×
  2 thresholds) and 15m-only. A different form (taker-buy imbalance, quote-volume
  z, cross-asset volume) or a different timeframe is untested.
- **Does NOT:** touch or contradict the standing finding that the EXIT dominates
  the entry (`docs/proposals/` asymmetry work) — it reinforces that rebuilding an
  entry rule is not where the leverage is.

## 5. Consequence for the loop

The `explore` item in `improvement_loop.py` should be annotated as TESTED-NEGATIVE
so the loop stops re-proposing it. This doc + the dated artifact are the evidence.
