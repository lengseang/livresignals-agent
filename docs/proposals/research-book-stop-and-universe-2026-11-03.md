# The research book: what its stop does, and which universe its guard compares

**2026-11-03 · daytime autonomous improvement pass.** Read-only on the book; one
new test file; one lesson. No order path, no parameter, no disable list, no
state file touched. Nothing is promoted and nothing is paused.

## Why this pass exists

The morning pass (#21, `incumbent-is-not-a-challenger-2026-11-03.md`) fixed the
loop refusing the incumbent as its own challenger, and left two things
unmeasured in its own text:

1. The LIVE row sits **above its own leave-one-coin-out ceiling**. Is that
   stable, or is it the draw?
2. The loop prints a −10% hard stop over 341 trades. Nobody has said what that
   stop is *doing*, or why 34.26 / 31.83 / 30.95 are treated as three facts.

One instrument answered both: a re-derivation of `bt()` that records two fields
the harness never does — **why** each position closed, and the **worst adverse
excursion** while it was open. It reproduces the printed book exactly:
`49.07 / 6.66 / 26.47 / 42.05`, median `34.26`, 341 trades.
`test_the_traced_book_is_the_book_the_loop_prints` enforces that on every run —
a copy that cannot reproduce its baseline has measured nothing (#14/#15).

## Finding 1 — the −10% stop is a floor on the tail, not a loss distribution

Exit reasons over the 341 trades: `hold 128 · sma50 95 · stop 118`.

- All 118 stop exits book at **−10.18% or worse**. Mean **−14.28%**, worst
  **−32.21%**. Not one books a gain, and none books "at" −10%: the loop sees
  only day-start prices, so the stop is detected on a close already through
  the line and booked at that close.
- **Not one of the 341 trades ever saw** a −10% adverse excursion
  (`count(MAE <= −10%) == 0`). The median worst excursion is **−1.35%**.
- Therefore the stop cannot change the win rate of the trades that reach it —
  every trade it closes is a loss by construction — it sets the **size of the
  118-trade tail**. Win rate 37.2% (127/341); among the 223 trades that closed
  by the hold clock or the SMA50 rule it is 57.0%, i.e. the stop accounts for
  the entire gap between the two. Any future "tighter / wider stop" argument
  on this book is an argument about tail size, and `stop=0.10` is not and never
  was a 10% loss cap — the label should stop reading like one. Same conditional
  structure #20 measured on the paper book (249/289 never near the stop), now
  confirmed on the research book with a baseline that reproduces.

## Finding 2 — `lead_is_coin_fragile()` compares a band with a row from a different universe

`run()` leaves the LIVE row's `fragility["per_coin"]` at **12 seeded draws over
22 non-BTC coins**. `lead_is_coin_fragile(chal, live_row)` then reads
`live_row.get("median")` — the **24-coin** median — and compares it with
`lf["loo_min"/"loo_max"]`, the **22-coin** band. The two sides of trigger (a)
are measured on different universes, and the direction of the error is decided
by which coin the seeded draws happened to drop, not by the config.

The same defect runs the **"LIVE EDGE DECAYED"** sentence the owner receives.
Measured today, the 12 draws collapse to **8 distinct universes**, and the band
is a min/max over them:

```
drop DOGE (3 draws) 7.47  |  LTC (2) 30.20  |  SOL (2) 27.84
INJ 24.84  ATOM 28.47  ADA 25.71  SUI 24.46  ETH 29.40
```

- The **floor 7.47 is three copies of one draw** (DOGE), one of them at the
  first position of the shuffle — exactly where LOO never lands in 6 of 12
  draws.
- The **ceiling 30.20 is the LTC draw**, i.e. the luckiest name, for the same
  reason. 34.26 sits above it: today's "regime break, not decay" verdict on a
  research simulation is produced by the **min/max of 8 draws**, not by the
  distribution.

**Corrected, in place, against today's real data:** a *full* leave-one-out —
drop each of the 23 non-BTC coins once, no sampling — gives medians
`15.04 … 46.57`, and the LIVE 34.26 is **inside** that range (only XRP, 15.04,
sits below 20; 6 of 23 drops are below 28). The band the loop prints is a
sampling artefact in **both** tails. No verdict flips today, because the loop
is silent on a crowning of the incumbent — but the decay sentence, which is
one refresh away, is currently computed on 8 of 23 universes and quotes
min/max as if it were an interval.

**Not fixed in the script.** The repair is small and changes the numbers the
owner would be shown (`LOO_DRAWS` up to ≥23 with a fixed de-dup, or exhaustive
drop-one over 23 coins; band reported as a quantile interval, not min/max), and
that is a behaviour change the loop's own charter puts behind a design note.
Proposed for the next research task.

## Finding 3 — why the crown moved: 2.4-3.3pp, inside the width of one coin

Crown `sma_r=150` **34.26** · `sma_r=200` **31.83** · `sma_r=100` **30.95** —
same names, same exit rule, differing only in the BTC SMA used for the bear
switch. Their margins are **2.43pp** and **3.31pp**, against a **9.64pp** spread
in the LIVE config's own leave-one-coin-out medians. Both margins are inside
the movement caused by dropping one name.

By the loop's own rule (a lead inside the LIVE config's LOO band is not an
edge) neither sibling could be announced even if it out-scored the crown; the
rule is never applied to a crown's neighbours, because a neighbour that wins is
refused and one that loses is not a challenger. The 100/150/200 ranking is
therefore decided by a difference the ranking's own instrument cannot resolve.
"Any refresh that moves a handful of daily bars can flip 34.26 vs 31.83" is
the causal description; "the cache re-ranked the grid" is only the surface.

Two caveats stated so this cannot be over-read in either direction: the
one-coin band's *width* is itself drawn from the same 12 seeded draws, so it is
an order-of-magnitude, not a test; and where the crown and `sma_r=200` differ,
the crown wins 3 of 5 non-identical 150d segments — "not distinguishable", not
"200 is worse".

## What this pass did NOT do, and one correction to the morning entry

- **No script change.** `scripts/research_loop.py`, `research/06-walkforward.py`
  and the cache are untouched; the findings file would regenerate to the same
  "no change (silence by design)".
- **Correction to the 2026-11-03 01:30 JOURNAL entry, and to the proposal's
  §"What this pass did NOT claim".** Both say the research book has "no fees".
  It has fees: `FEE = 0.00144` (0.144% round trip, BNB-discounted spot) at
  `research/06-walkforward.py:31`, charged on every exit. What the book lacks
  is **measured spread/slippage** and a **shared-account constraint** — a
  narrower and less alarming caveat. The morning text erred in the more
  pessimistic direction, which is still a mis-statement.
- **Not measured:** slippage beyond the flat fee. Finding 1 says the flat fee
  *under*states cost exactly on the 118 tail trades, where a −32% booking
  through a gap is also a fill well past the stop. That is a fill-model
  question for the T4.1 gate, not a research-loop one.
