# The research book's 2% risk rule is a cap, not a budget — and the crown is mostly a function of that cap

**2026-11-04 · overnight improvement shift.** Read-only on every book, every
parameter, every state file. No sizing change, no promotion, no pause, no config
change. Nothing here touches an order path. The research loop was run through its
normal entrypoint once (01:02), which regenerated its own two files, and the
finding was sent to the `reports` topic (message 5500) because it changes what a
printed number means.

## Why this pass exists

Yesterday's pass made the leave-one-coin-out band exhaustive and, in doing so,
found that the decay test could never fire. It left the next step as "decide what
decayed means" — and I did not do that. I went looking for the reason the
instrument is so flat, and the reason is not the band.

## 1. Arrival state was red, and it was cache drift, not a defect

Full suite on arrival: **1 failed** —
`test_research_stop_basis.py::test_the_traced_book_is_the_book_the_loop_prints`,
`assert 36.11 == 34.26`.

The 01:00 cache refresh rolled the window one bar: 1 bar in, 1 bar out, on all 24
coins; **every date key shifted by exactly one** (`19761..20760` →
`19762..20761`). The walk-forward windows are *offsets*, not dates, and the LIVE
config's fold 1 tops out at 113% of its fold capital (60% BTC, 30% BNB), so its
outcome is highly sensitive to where the window starts. `36.11` is the same book
one bar later. This is the third time a test has pinned a level on a cache
refreshed daily; the pin is now recorded here rather than rediscovered.

## 2. The finding: `size = min(cash·risk/σ, cash/topn)` — the risk term never binds

`bt()` sizes a position as `cash * risk / (sd20/px)` capped at `cash/topn`,
**then rejects below `sz < 0.05`**. Three consequences, all measured on the
2026-11-04 book:

- **The ratio distributes the whole book.** σ/px for liquid coins is ~0.03–0.05,
  so `risk/σ` is ~0.2–0.35 — 4–7× the `1/3` cap. When three names are already
  held the cap is `cash/1`, so a new name takes up to **60.5% of equity**
  (BTC, day 289). Median size 10.7%, smallest 5.4%.
- **A 60% position falling 10% is −6% of equity.** The stated 2% is the
  *smallest* risk the rule can express, not the target; the rule is a cap.
- **The 5% floor is reachable and it matters.** 79 of 439 candidate entries were
  rejected as "too small" — all of them with `raw < cap` (never for lack of
  room). At a 3% floor: **0 skips**, and the book moves to
  `54.28 / 5.36 / 26.42 / 39.65`, median **33.04** vs **36.11**.

## 3. What that does to the crown

The same entries, re-sized under one alternative each time:

```text
as printed              36.11
no 5% floor             33.04   (-3.07)
no topn cash cap        36.95   (+0.84)
max size 30%            36.36   (+0.25)
max size 15%            30.59   (-5.52)
equal weight, cap 100%  14.68   (-21.43)
```

Every knob produces a different winner-set and the same nine-config grid is
ranked on it. **A nine-config grid whose ranking moves 21pp under a sizing rule
the grid does not vary is ranking the sizer.** The ranking is not evidence about
the signal — and neither is the LIVE-vs-BTC headline (+54.6pp) that the loop
ships nightly.

**Not fixed here.** Changing sizing after reading these numbers is the
pre-declared-validation error this journal has recorded four times. The list
above is the menu; picking from it needs a decision made before the next
look at the book.

## 4. The LOO band is [X, X], and it is not a coincidence

With the universe as the only thing `coin_fragility()` varies and one config
fixed, on this book every one-coin drop returns the same median:
`loo_min == loo_max == 36.11` (`draws 24 == distinct_universes 24`).
`live_fragile` is then `median != median`. Yesterday's pass reported
`[7.47, 45.64]` on the 2026-11-03 book — a 38pp band — on a cache **one bar
different**. The spread was not fragility; it was one-coin sensitivity of the
*sizing*, which §2 says is window-dependent.

So the band's two states are "cannot fire" (2026-11-03) and "cannot fire"
(2026-11-04), for different reasons. Pinned by
`tests/test_research_loop_flat_band.py` (2 passed, 1 skipped — the skip says
out loud that the band is degenerate today rather than hiding it behind a
hardcoded number a refresh would red-line).

**Probe that did NOT pan out, recorded so it is not repeated:** I first suspected
the return was a one-off re-rating of the basket (BTC's window-start at 45k vs
its end at 69k). An equal-weight buy&hold of the same 24 coins does not co-move
with the book, and booking every position at its entry price (zero wedge) loses
only 4.6pp of the median. The return is not a start-bar artefact.

## 5. What is NOT claimed

- **No claim about which config is best**, and none about whether the live
  config has decayed.
- **No claim that the 2% is wrong.** It is a *documentation* defect with a
  measured consequence; whether to make it a budget, a cap with a cash reserve,
  or an equal-weight slice is an owner decision.
- **No sizing change was made or is proposed as implemented.** §3 is a menu.
- **The 2026-11-03 write-up's numbers are not retracted**; they are about that
  book. What is retracted is the implication that the band measures decay.

## 6. Verification

```text
venv/bin/python -m pytest -q tests/test_research_loop_flat_band.py
  -> 2 passed, 1 skipped (skip = band is degenerate today; message says so)
venv/bin/python scripts/research_loop.py
  -> best median 36.11% (4/4) | live median 36.11%
  -> CHANGE: NEW BEST ... sma_r=150  (the incumbent re-crowning itself after a
     one-bar refresh — see §1; the loop's own "news" is cache drift)
```
