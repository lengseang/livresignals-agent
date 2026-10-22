# The exit advice was computed on the half of the book that cannot trade

**Date:** 2026-10-22
**Status:** implemented (improvement_loop)
**Tests:** `tests/test_improvement_pf_live_population.py` (10)

## What the owner was reading

`reports/improvement-report.json` (09:30Z run), priority 2, every day:

```
Profit factor 0.97 < 1.5. Consider raising TP:SL ratio (2:1 -> 2.5:1)
or tightening the stop.
```

Two independent things are wrong with that one sentence.

## Defect 1 — the premise was computed on edges the report had just buried

`paper_stats["profit_factor"]` is formed over **every** closed record. Split
the same collapsed book by liveness (`paper_book.edge_liveness`, the rule added
yesterday) and the figure decomposes like this:

```
            n     net      PF
whole     2637   -2.24    0.97
live      2365   +3.44    1.05   (loose, validated)
dead       272   -5.68    0.47   (supertrend, bollinger, momentum,
                                  ema-cross, breakout, pullback, rsi2)
```

The same report carried, at priority 3, an `edge_not_live` note naming seven of
those nine edges as "HISTORY, not the live book — no fill in >14d". Their 272
trades lost $5.68 and dragged a **net-positive** live book (PF 1.05) down to
0.97 — *below 1.0*, i.e. the card asserted the account loses money before
costs, which was false for every trade it could still take.

This is SYSTEM_MAP rule #14 (a per-edge verdict is about an edge that is still
trading) applied to the last place that still pooled the whole book: **a rule's
own premise must be computed over the population that rule is allowed to speak
about.** The per-edge rules were fixed yesterday; rule 3 was the aggregate
header above them and was left reading the corpse pile.

## Defect 2 — a gross-era bar applied to a net ratio

`profit_factor` has been NET since the 2026-10-11 net switch. The 1.5 bar
predates it and was calibrated on a **gross** book. On a ~2:1 design the
measured 0.1545% round trip takes ~12% off each win and adds ~24% to each loss,
which pulls PF down by roughly a third — so 1.5-on-net is a bar the book has
cleared on **zero** runs since the switch.

A threshold nothing can meet is an always-on warning, and this repo already
learned what that costs (2026-10-13, tag freshness: "a warning that is always on
is indistinguishable from no warning, and it MASKS the genuine freeze").

## Fix

1. **`_split_live_pf(edges, edge_meta)`** — recomputes PF separately on the
   live and not-live populations. PF is rebuilt from **win/loss dollars**
   (`gross_win`/`gross_loss`, now emitted per edge row), never from a net sum:
   an edge at net −0.13 after +29.14/−29.27 is PF 0.995, not "PF 0".
   Accepts either `edge_meta` or a bare `last_fill` map — passing the bare map
   used to make every edge undatable → "live" → the split silently collapsed
   back to the whole book, reproducing the exact defect.
2. **Rule 3 reads the live population**, states it (`… on the live: 2365
   trades, +3.44 net, 2 edge(s) (loose, validated)`), and, when the excluded
   dead population is material, names it (`NOT counted: 272 trades from 7
   edge(s) with no fill in >14d (−5.68 net)`). A suppression must leave a trace.
3. **`PF_NET_FLOOR = 1.02`** — "does the live book clear its own costs at all".
   Set just above break-even: the measured live book pays at 1.05 and must not
   be flagged; 1.10 would have flagged a profitable book (the old 1.5 error in
   the other direction).
4. **`PF_MIN_LIVE_N = 20`** unchanged, now applied to the live sample.
5. No liveness data at all (reader import failed, empty book) **degrades to the
   whole-book reading** and says so — the rule is never silenced by a broken
   reader.

## Measured effect

Before: `0.97 < 1.5` — a false panic about a paying book, on 100% of runs.
After:  no `rr_tune` line (the live book pays). Verified on the real ledger:
`live PF 1.05 / dead 0.47 / whole 0.97`, ten suggestions, no rr_tune. Negative
control (a single live edge at −$6.00) fires correctly at `0.81 < 1.02`.

## Deliberately NOT done

- **No edge was re-enabled or disabled.** `validated` still draws its
  net-negative decay verdict (it is live, it is losing—just $0.13).
- **No threshold was tuned on the strategies.** The `loose` preventive note and
  the regime data-gap note are untouched.
- **`rr_tune` was not deleted.** It is unconsumed by code and the openers take
  TP:SL from the design, not a knob — but it is still the only place the
  aggregate "is the book paying" question is asked, so it now asks it honestly
  rather than being removed.
