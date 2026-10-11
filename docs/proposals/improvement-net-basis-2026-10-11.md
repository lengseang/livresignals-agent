# The improvement layer judged edges on GROSS P&L and called it net

**Date:** 2026-10-11
**Class:** stale-basis / silent-optimism defect (same family as the stale edge
feed, the missing regime tag, and the frozen 0.03% cost constant — all fixed on
2026-10-10/11, but this third consumer was missed)

## What was wrong

The 2026-10-11 cost fix (`docs/proposals/paper-cost-measured-2026-10-11.md`)
made the paper book charge the MEASURED exchange round trip — **0.16092%**, or
**5.4x** the stale 0.03% constant it had used. That fix was propagated to
`unified_trader` (the writer) and `self_improve` (which now reports NET), and
`scripts/paper_net.py` was added as the single honest reader.

But two other consumers of the same paper book were left summing raw gross
`pnl`:

| file | what it does with the number |
|---|---|
| `scripts/improvement_loop.py` | the per-edge promote/demote verdicts — the loop's *"accuracy-improvement core"* |
| `scripts/night_tuner.py` | `_stats()` summed gross into a field **labelled `net`**, feeding a real-money stop-loss adjustment |

Neither was cosmetic. Read on the live book, the improvement report's headline
flips from a profit to a loss when the basis is corrected:

```
basis     n     WR      PF     total
gross     2858  47.3%   1.23    +$18.73
net       2858  42.3%   0.97    -$2.69
```

And the per-edge verdicts flip sign at the individual level:

```
edge         n     gross     net     verdict change
loose        1657  +17.65    +5.43   (still positive, much thinner)
validated     839   +4.14    -2.17   FLIPS: was credited, is losing
momentum       30   +0.19    -0.05   FLIPS: was "alive", is bleeding
supertrend    191   -0.70    -2.20   (worse than reported)
bollinger      53   -0.61    -1.03
```

An edge whose true edge is thinner than the round-trip fee was being reported
as profitable, and could be promoted toward real money on that basis. With the
correction, `momentum` is correctly flagged DECAYING (it was a false positive
on gross) and the book's own profit-factor drops below 1.0.

## The fix

Both files now read the paper book through the same single reader the writer
uses, `scripts/paper_net.py`:

* `improvement_loop._stats()` charges the measured round trip and reports
  `total_pnl` (net) beside `gross_pnl`, with `pnl_basis: "net"` stamped into the
  report so a reader cannot mistake the basis. Per-edge rows carry both
  `net_pnl` and `gross_pnl`; the promote/demote rules read net.
* `night_tuner._stats(rows, net=True)` — paper windows (24h / 72h / per-edge)
  are now net. **Live rows are deliberately NOT costed**: a real fill's P&L is
  already net of the fee the exchange charged, so applying a modelled cost on
  top would double-count it (pinned by a test).
* Both imports degrade to the old gross behaviour if `paper_net` cannot be
  imported, so a broken import cannot crash a cron job.

`pnl` (gross) is unchanged in the data, so every existing consumer still works;
only the *verdict basis* moved.

## Guardrail / non-regression

`tests/test_improvement_net_basis.py` (6 tests) pins:
* the loop reports net below gross, and the marginal gross winner can drop out
  of the net win-rate;
* a thin gross-positive / net-negative edge is flagged DECAYING (it was not,
  on gross);
* the report declares `pnl_basis == "net"`;
* `night_tuner` charges cost when `net=True`, and does NOT double-cost live rows;
* the paper windows are wired to `net=True` and the live window is not.

## Scope note

This is a *measurement* correction, not an alpha change. It does not make the
strategy profitable — it stops the improvement layer from telling the owner an
edge is profitable when the exchange has already taken the difference. The
open question it sharpens is unchanged and remains the top research item: the
sharp-dip entry is negative-EV at real cost, and no exit tuning rescues it.
