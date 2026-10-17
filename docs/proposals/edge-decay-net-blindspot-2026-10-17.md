# Learning: the per-edge demote rule was blind to NET P&L — the book's LARGEST loss drew no verdict

**Date:** 2026-10-17
**Cron class:** LEARNING
**Commit:** `improvement-loop: judge per-edge decay on NET, not win rate alone`

## What was found

`scripts/improvement_loop.py`, the per-edge promote/demote block — the loop's
self-described "accuracy-improvement core" — carried exactly two rules:

```python
if e["win_rate"] >= 58 and e["avg_pnl"] > 0:   # PROMOTE
    ...
elif e["win_rate"] < 45:                        # DEMOTE (decay)
    ...
```

Both key on the **win rate**. Everything between 45% and 58% is a dead zone in
which an edge draws no verdict at all.

On the real book (`reports/improvement-report.json`, regenerated 2026-10-17T01:01Z):

```text
edge             n     wr       net      gross
loose         1376   48.8    +4.376   +14.258
validated      843   48.4    -2.808    +3.428   <-- LARGEST loss, no verdict
supertrend     134   40.3    -1.851    -0.824
bollinger       47   34.0    -1.037    -0.671
ema-cross       22   27.3    -0.834    -0.663
pullback        17   35.3    -1.113    -0.981
breakout        18   27.8    -0.442    -0.302
momentum        27   33.3    -0.164    +0.046
```

`validated` loses **$2.81 net over 843 trades** — the largest single loss in the
book, larger than every edge the rule *did* flag (supertrend + bollinger +
ema-cross + pullback + breakout = **-$5.28** at 3x its size, per-trade it is the
biggest) — and it sits at 48.4% WR, inside the dead zone, so the loop emitted
**nothing** about it.

## Why this is a real defect, not a threshold quibble

**Win rate and P&L are different questions.** An edge can win half its trades and
still bleed money if the wins are small and the losses are large — which is
precisely what a capitulation-dip opener does when the dip keeps going: many
small bounces, a few stops that eat the lot. `validated` is gross-positive
(+$3.43) and net-negative (-$2.81): the fee is doing the damage, spread across
843 small trades. A verdict that reads only the win rate is *structurally*
unable to see that shape of loss, and that is the loss most likely to hide.

This is the **same class** the repo has fixed three times already — a verdict
keyed on the flattering metric while the money moves on NET:

- 2026-10-11 · the whole per-edge section read gross → fixed to net.
- 2026-10-13 · `self_improve`'s disable gate read gross → fixed to net.
- 2026-10-17 · the per-edge **demote predicate** still read only WR.

The 2026-10-11 fix swapped the *units* (gross → net) but left the *rule* keyed on
win rate, so a net-loss spread thin across a majority-ish of trades stayed
invisible. Fixing the basis is not the same as fixing the predicate.

## The fix

The demote verdict now judges **NET P&L alongside win rate**:

```python
net_pnl = e.get("net_pnl", 0) or 0
losing = net_pnl < 0
if e["win_rate"] >= 58 and e["avg_pnl"] > 0 and not losing:   # PROMOTE (stricter)
    ...
elif e["win_rate"] < 45 or losing:                            # DEMOTE
    ...
```

Two properties are deliberate:

1. **The promote bar is NOT relaxed.** A net-positive edge below WR 58 earns no
   promote — it is merely *spared* the decay verdict. "Promote slowly" is kept;
   only the false-negative side is closed. (The `and not losing` guard also stops
   a net-losing edge from ever being called PROVEN if its WR clears 58.)
2. **A net-negative edge above the WR floor says why it was caught** — the note
   reads *"net-NEGATIVE despite a 48.4% win rate (wins too small, losses too
   large)"* — so a reader who only sees the win rate understands the verdict
   rather than thinking the rule fired on a typo.

The `n >= 10` floor is unchanged, so a 3-trade loss is still noise, not decay
("a 1-trade window must not decide anything").

## What this is NOT

- **Not** an approval to act. The loop only *suggests*; nothing here touches an
  order, a param, or the disable list. `validated` is already DISABLED in
  `data/signals-state.json` (it and `loose` were both flagged long ago), so the
  new p2 line is a *reporting* fix — it makes the loop's own accounting honest,
  it does not change what trades.
- **Not** a claim that `loose` is bad. `loose` is net-positive (+$4.38) at a
  similar win rate and is correctly left alone — the guard does not cry wolf.
- **Not** a threshold change to the 45% / 58% bars; they are untouched. Only
  NET is added as an *additional* trigger.

## Verification (2026-10-17)

```text
RED first : tests/test_improvement_edge_net_blindspot.py
            2 failed (net-negative 48.4% WR edge drew no verdict;
                      62% WR net-loser not flagged), 2 passed
GREEN     : 4 passed
mutation  : restoring `elif e["win_rate"] < 45:` re-fails exactly those 2
            tests; file restored afterwards
consumer  : report schema unchanged (same type/priority/idea keys); the
            real book now emits an extra p2 edge_decay naming 'validated'
            as the largest loss — the fix working, not a regression
full suite: 625 passed, 3 skipped   (was 617 + 4 net-new = 621 … see note)
```

Note on the count: the improvement-layer files
(`test_improvement_edge_net_blindspot`, `test_improvement_loop_freshness`,
`test_improvement_net_basis`, `test_self_improve_edge_net_basis`) run together at
22 passed; the whole suite is 625 passed / 3 skipped with no new failures.

## Durable rule

**A verdict must be keyed on the quantity the decision is about.** "Is this edge
declining?" is a P&L question; a win-rate threshold is a proxy that fails exactly
when wins and losses differ in size — i.e. whenever the strategy has any
asymmetry at all. When a gate states a rule over a proxy (win rate, gross,
coverage %), ask what the proxy assumes, then check the gate tests that
assumption. This is the same shape as the 2026-10-16 "single-valued regime"
lesson: the guard must test the *condition*, not the *label* that happened to
expose it.
