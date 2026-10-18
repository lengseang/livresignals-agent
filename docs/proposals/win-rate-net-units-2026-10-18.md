# Learning: a verdict that says "net" was counting its wins on GROSS — and with the true rate, the book's only winner drew "stand it down"

**Date:** 2026-10-18
**Cron class:** LEARNING
**Commit:** `eaf9a2e improvement-loop: count win RATE on net, not gross, wherever P&L is net`
**Files:** `scripts/improvement_loop.py`, `scripts/night_tuner.py`,
`scripts/self_improve.py`, `tests/test_win_rate_net_units.py` (new)
**Scope:** report/tuning layer only. No order path, no param, no disable list,
no paper book touched.

---

## 1. What was found

The per-edge table — the loop's "accuracy-improvement core" — computed

```python
e["pnl"] += _pnl_of(t)[0]          # NET of the measured round trip
if (t.get("pnl", 0) or 0) > 0:     # GROSS
    e["wins"] += 1
...
"win_rate": d["wins"] / d["n"] * 100,   # labelled "WR" in a verdict that says "(net)"
```

Two units in one sentence. `_stats()` for the whole book had counted net wins
since the 2026-10-11 fix (`netwins`); the per-edge and per-regime loops never
got it. Measured on the real book, regenerated 2026-10-18:

```text
edge         n     WR(before)  WR(after, net)  grossWR   net
loose      1376      48.8          44.1          48.8   +4.47
validated   878      48.6          43.6          47.5   -3.49
supertrend  134      40.3          31.3          40.3   -1.84
pullback     17      35.3          29.4          35.3   -1.11
rsi2          7      42.9          14.3          42.9   -0.24
```

`validated` read **47.5%** WR in a "-3.50 net" verdict; its net rate is **43.6%**.
The error ran the same way as the loss: the flattered metric sat directly beside
the flattered P&L. On `rsi2` the two units differ by **28.6 points** (42.9% gross
vs 14.3% net) — the gross figure is measuring "did price move my way at all",
which is not the question a fee-paying book is asking.

Same leak, two more consumers:

- `night_tuner._stats(net=True)` — net dollars, gross win rate; that figure
  feeds a **real-money** stop-loss adjustment (`LIVE_STOP_PCT`).
- `self_improve.stats_net_of` — `wr` was already net (correct), but the gross
  rate was not reported at all, so a reader had no way to see the haircut.

## 2. Why this is the fourth instance of one class

| date | fixed | left alone |
|---|---|---|
| 2026-10-11 | the **basis** (gross → net dollars) | the verdict keyed on WR |
| 2026-10-13 | `self_improve` **disable predicate** | the *win count* behind the rate |
| 2026-10-17 | the per-edge **demote rule** (added net) | the *win count* behind the rate |
| 2026-10-18 | the **win count** itself | — (this pass) |

Each pass fixed the figure it was looking at and left the next derivation of the
same quantity in the old units. **Fixing a basis is not fixing every figure
computed from it** — the check is "does every number in this sentence answer the
same question?", not "did I change `pnl` to `pnl_net`?".

## 3. The consequence that mattered more than the defect

With a true net win rate, `loose` — **+4.47 net, the only net-POSITIVE edge in
the book** — fell under the `WR < 45` floor at 44.1% and drew:

```text
Edge 'loose' DECAYING (net): 44.1% WR / 1376 trades / +4.47 net ...
  Tighten its entry or stand it down — it is losing edge.
```

**The honest metric produced a dishonest verdict.** The 45% floor is a
*heuristic*: "this win rate is probably too low to pay the fee". When the
measured net says the edge pays, the heuristic is refuted by the money. The fix
does not delete the flag — the owner should still see a sub-floor win rate — but
splits the two routes into two sentences and two priorities:

```text
net-NEGATIVE at any WR   -> p2  "losing edge ... stand it down"      (action)
net-POSITIVE, WR < 45    -> p3  "PREVENTIVE only ... keep measuring" (advisory)
WR >= 58, net positive   -> p2  PROVEN                               (untouched)
```

The promote bar is deliberately **not** relaxed: a paying edge below 58% WR is
not promoted, it is merely spared the stand-down — the repo's "promote slowly"
rule, applied consistently.

Not applied to `self_improve._plan_disables`: that predicate requires WR < 42
**and** net < 0 **(and n >= 15)**, so a paying edge cannot be disabled by it
(verified on the real book: `loose` n=83, net -0.353 → flagged; `validated` is
the held-back survivor). A rule that already requires a net loss does not have
this defect.

## 4. Verification

```text
RED first : tests/test_win_rate_net_units.py   5 failed, 1 passed
            (per-edge, per-regime, night_tuner net branch, self_improve
             wr_gross; the 6th pins paper_net.summarise, which was correct)
GREEN     : 7 passed
mutation  : restoring "if (pnl) > 0: wins += 1" in the per-edge loop and
            "win_rate": w in night_tuner's net branch re-fails exactly the
            per-edge + night_tuner tests; files restored afterwards.
full suite: 638 passed, 3 skipped  (was 631 + 7 net-new)
E2E       : improvement_loop.py regenerated reports/improvement-report.json from
            the real book; every by_edge / by_regime row now carries both
            win_rate (net) and win_rate_gross. night_tuner._stats on the last
            100 paper trades: gross branch 46.0% WR, net branch 37.0% WR /
            -1.006 net, with win_rate_gross 46.0 carried.
```

The fixture cost constant is read from `paper_net.PAPER_COST_RT` (0.154%),
not hard-coded, so the tests cannot silently drift from the writer's fee basis.

## 5. Lesson

**A decision rule is only as honest as the unit of the least-honest figure in it.**
Four passes fixed the dollar basis three times before anyone asked whether the
*win rate* in the same sentence was computed the same way. The generalisable
check: for each verdict, list every number it publishes and ask of each one
"gross or net?" — if the answer is not uniform, the verdict is comparing two
different worlds.

Second, and more useful: **a fix can expose a rule whose threshold was tuned
against the wrong number.** The 45% floor was never wrong as a heuristic, but it
had only ever been fed a gross rate; fed the true rate, it condemned the only
edge that pays. When a measurement is corrected, every threshold downstream of it
must be re-derived, not just re-run.
