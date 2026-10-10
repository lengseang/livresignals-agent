# Which class of edge is not yet ruled out?

**Date:** 2026-10-10 · **Status:** decision record

## What today ruled out

Every measurement taken on 2026-10-09/10 points the same way.

| measurement | result |
|---|---|
| hunt, 48 entry families x 6 exits, 2025 | 0 above PF 1.0; best 0.950 (n=65, noise); median 0.792 |
| asymmetry test, buy-dip vs fade-rip, 2025-26 | best 0.888 |
| stop/hold isolation | PF rises with stop width and hold, but plateaus at 0.848 |
| the live book, 30 real trades | 40% win, net -$0.29 |
| the paper book, 2,751 trades | +19.4 gross — but no real fills, optimistic |

**The class ruled out: high-frequency mean-reversion dips, 5m-30m, 10-20k trades/year.**
It is fee-bound. At 0.10%/side a round trip costs 0.30%, and no rule in this family
produces an edge larger than that. Tuning parameters moves PF by ~0.10; it does not
cross 1.0. This is the signature of an absent edge, not a mis-tuned one.

## The structural reason

Frequency and fee are multiplicative. 20,000 trades/year at 0.30% round trip is
60x the account's face value paid in fees. The rule would need a per-trade edge
above 0.30% to survive, and the measured per-trade edge is ~0.01-0.05%.

## What is NOT yet ruled out

1. **Low-frequency, high-conviction.** Fewer, larger moves. A trade held days/weeks
   whose expected move is 5-15% can absorb 0.30% of fees. The hunt has not searched
   this: every candidate fired 2.5k-22k times/year. **Search space: fire 20-200
   times/year.**

2. **Trend-following / breakout on higher timeframes.** The breakout family in the
   hunt (20/50/100-bar highs) scored 0.85-0.86 on 15m at ~3k trades/yr. That is the
   *best* non-dip family found, and it was only tested at 15m with a 96-bar hold.
   Daily bars were never tested at all.

3. **Regime-conditional.** Trade only when a regime filter says the environment is
   favourable, and do nothing otherwise. Zero trades in bad regimes is a legitimate
   result. Never tested.

4. **Directional asymmetry (the down-run finding).** Down-runs last up to 16 bars vs
   11 up. A long-hold short-side or a "fade the rip" rule was tested and scored 0.888
   — better than buy-the-dip, but still below water. Worth one more look at higher
   timeframes where the fee is a smaller fraction of the move.

## The next experiment (pre-registered before running)

**Hypothesis:** an edge exists, if anywhere, at LOW FREQUENCY, where the fee is small
relative to the move.

**Gate:** every candidate must fire **< 500 times/year across all symbols**. Any
candidate above that is rejected outright — it is the class already ruled out.

**Search:**
- timeframes: 4h, 1d (never tested)
- families: breakout (N-bar high), trend (SMA cross / slope), regime-gated dip
- exits: wide stops (4-8%), targets 2-4x stop, holds 10-60 days
- costs: 0.10% fee + 0.05% slip per side, always charged
- split: IS 2017-2023 / OOS 2023-2026, positive in BOTH required

**Falsification (written before running):**
- H1 low-frequency candidates fire < 500/yr AND beat PF 1.0 in both halves
  -> if none do, the low-frequency class is also empty and we say so
- H2 if a candidate survives, its trade count must be high enough to be meaningful
  (>= 100 trades OOS), or it is noise and is discarded
- H3 the fee must be < 20% of the average winning move, or the edge is fee-bound
  regardless of what PF says

**Owner decision 2026-10-10:** stop real trading (C) + keep hunting (B). Live switch
removed: `config/LIVE_ENABLED` -> `config/LIVE_ENABLED.frozen-2026-10-10`.
Open positions: none. Capital: $20.12 USDT, intact.
