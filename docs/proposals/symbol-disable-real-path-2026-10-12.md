# Learning: the real-money path ignored the symbol it had stood down

**Date:** 2026-10-12
**Cron class:** LEARNING
**Commit:** `unified_trader: enforce SYMBOL disables on the real-money path`

## What was found

Yesterday's fix (`00cb03e`) made the three NATIVE paper openers read the
self-improve EDGE disable list. Tonight's is the same defect one layer deeper —
on the **real-money** path, where the quantity ignored is a *symbol*.

`self_improve.py` (and the ops process) stands symbols down by writing
`data/signals-state.json['disabled_symbols']`:

```
disabled_symbols: ["ARB/USDT", "SOL/USDT"]
```

`_symbol_disabled()` is honoured by the paper openers. **The real-money tier
gate `_real_tier_ok()` never read it.** It checked only:

```python
if sym not in ELITE_SYMS:      # ETH/SOL/XRP
    return False
if not sig.get("sharp"): ...
```

The three real-order call sites (`unified_trader.py` ~840 validated, ~858 loose,
~870 pullback) all pass through `_real_tier_ok`, so **one missing check covered
all of them**.

## Why it only just became live

The `_symbol_disabled` docstring read *"Paper-only impact: ARB is never real (not
in ELITE/REAL_TIER)"* — true when ARB was the only entry. That assumption was
silently broken the moment **`SOL/USDT` was added to the list, because SOL IS in
`ELITE_SYMS`**:

```
ELITE_SYMS       : ETH/USDT, SOL/USDT, XRP/USDT
disabled_symbols : ARB/USDT, SOL/USDT
INTERSECTION     : SOL/USDT          <- the one that matters
```

**Evidence it was live, not theoretical** — the real spot book
(`data/live-trades.json`) has two SOL closes on 2026-10-12 while SOL sat on the
disabled list:

```
SOL/USDT  2026-10-12T04:16  pnl +0.0693  reason STALL
SOL/USDT  2026-10-12T11:56  pnl -0.0180  reason TIME
```

A symbol the system's own negative-knowledge record had stood down was still
being sized with real money.

## The fix

Two readers, both the single chokepoint for their venue:

1. `_real_tier_ok()` — added `if _symbol_disabled(sym): return False`. Covers
   every real-order call site. **Fail-OPEN**: a missing/unreadable state file
   still returns `False` from `_symbol_disabled`, so a corrupt file never blocks
   trading (unchanged behaviour).
2. `futures_open()` — the futures branch calls it **directly**, bypassing any
   outer gate, so it now reads the same `SIGNALS_STATE` key. This mirrors the
   existing `futures_sharp_dip_paused` enforcement, whose comment already states
   the rule: *enforce HERE so EVERY caller is protected*. SOL is also in the
   futures allowlist (`SPOT_TO_FUT`), so it was exposed there too.

## Why it matters

A control the system writes that the money path does not read is worse than no
control: the ops report says the symbol is off, the self-improve list says it is
off, and the exchange fills it anyway. The failure is invisible because both
halves look healthy in isolation — the exact pattern named in yesterday's
edge-disable lesson, now shown to generalise from *edges* to *symbols* and from
*paper* to *real*.

## The generalisable lesson

**After the disable is wired into the reader you were looking at, ask which
other venue has its own copy of the decision.** The edge fix closed the paper
openers; the symbol list was read by the same openers and looked done — but the
real and futures paths are *separate code* that make the same "may I open?"
decision. **A disable list with N consumers needs N readers.** Grep the state
key, not just the function you just fixed.

## Verification

- `tests/test_symbol_disable_real_path.py` — 5 tests: the real tier gate blocks
  a disabled ELITE symbol, allows a non-disabled one, is fail-OPEN on a missing
  state file, and re-opens when the symbol is removed from the list; plus
  `futures_open` blocks before any network call.
- **Mutation-checked** (git-show A/B, per the git-state recipe): reverting both
  fixes makes exactly the 2 fix-pinning tests FAIL
  (`test_real_tier_ok_blocks_disabled_symbol`,
  `test_futures_open_blocks_disabled_symbol`) while the 3 fail-open controls
  still pass; restoring the fix returns 5/5. Fixed files restored and diffed
  against HEAD to prove the restore.
- Full suite: **581 passed, 3 skipped** (baseline 576 + 5 new), 0 failures.
- End-to-end on real state: `_real_tier_ok("SOL/USDT", …) -> False` (disabled)
  while `ETH/USDT` and `XRP/USDT` -> `True` (untouched). Live
  `unified_trader.py` scan ran clean.
- `config/LIVE_ENABLED` unchanged (empty file, presence intact) — the fix only
  ever BLOCKS entries, never creates one.
