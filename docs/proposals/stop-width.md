# Proposal: bound the initial stop distance (supersedes the RR floor)

> **Status: PROPOSAL — not applied, not approved, no order-path change made.**
> Supersedes `docs/proposals/rr-floor.md`, which is retained for its validation run.
> Read §6 of the RR proposal first: the RR effect is real but it is a **proxy**.
> This document identifies the underlying variable.

---

## 1. Summary

**The initial stop distance — not the reward:risk ratio — is the variable that separates
profitable trades from losing ones in the paper book.** The RR floor proposed earlier
correlates with it and would have *partially* worked, while leaving the true cause in
place.

```
tight stop (<1.5%)   n=1,664   net +54.549   avg +0.03278
wide  stop (>=1.5%)  n= 673   net −32.285   avg −0.04797
```

**The entire paper-book loss sits in the 673 wide-stop trades.**

---

## 2. The mechanism

A wider stop does what it is designed to do — **it reduces stop-outs** (48.3% → 29.1%).
That part of the original reasoning was correct. What was never measured is **what
replaces them**:

| | tight (<1.5%) | wide (≥1.5%) |
|---|---|---|
| SL exits | 803 (48.3%) | 196 (29.1%) |
| **SL avg per trade** | **−0.02377** | **−0.13121** |
| **SL win rate** | **25.7%** | **0.0%** |
| TIME exits | 296 (17.8%) | 451 (67.0%) |
| **TIME avg per trade** | **+0.00625** | **−0.02148** |
| **TIME win rate** | **66.6%** | **27.5%** |
| STALL exits | 237 (14.2%) | 7 (1.0%) |
| **STALL avg per trade** | **+0.05822** | **+0.01740** |
| TP exits | 326 (19.6%) | 19 (2.8%) |

**Three findings:**

1. **Each stop-out costs 5.5× more** under the wide regime (−0.131 vs −0.024). This is
   partly arithmetic — a 2% stop loses twice a 1% stop — but the measured ratio is worse
   than 2×, and **every one of the 196 wide stop-outs is a full loss (0.0% win rate)**.

2. **Stop-outs are replaced by timeouts, and timeouts flip from green to red.**
   Tight timeouts win 66.6% and average +0.006; wide timeouts win 27.5% and average
   −0.021. **The wide stop keeps a dying trade alive until the clock closes it in the
   red.**

3. **The lock-half ratchet stops working.** `STALL` exits — where price stalls and the
   ratchet has locked in a gain — run **237 under the tight regime (+0.058/trade) and
   7 under the wide regime (+0.017/trade).** A stop wide enough to survive noise also
   gives back the move the ratchet would have captured.

**H3 (new, and the most important):** the wide stop does not merely cost more per loss —
**it disables the exit machinery**. `TP` falls from 19.6% to 2.8% and `STALL` from 14.2%
to 1.0%. With a 2% stop the trade has so much room that neither the target nor the
ratchet is reached before the time stop.

---

## 3. Where the wide stops come from

Not drift, and not a bug found — **two stop regimes run concurrently**, by design:

```
scripts/unified_trader.py:178   stop = _exit_params()["stop_pct"]    (adaptive)
scripts/unified_trader.py:180   stop = 0.02                          (fallback)
scripts/unified_trader.py:325   stop = 0.012                         (_pullback_open)
scripts/unified_trader.py:426   stop = 0.012                         (_paper_loose_open)
```

`_paper_open` uses 2% (or the adaptive value); the pullback and loose paths use 1.2%.
**Daily medians oscillate between 1.199% and 2.000% depending on the mix of paths that
fired** — Sep 23, Sep 29, Oct 1, Oct 3 and Oct 4 all show a 2.000% median; Oct 5 is back
to 1.199%.

**The 2% choice is documented and deliberate** (`unified_trader.py:163-169`):

> *at 1.0% stop 158/508 exits were full stop-outs and only 69 reached TP; at 2.0% only
> 65 stop out and 84 exit +0.8% — positive in 13/14 symbols*

**That measurement is not wrong; it is incomplete.** It counted stop-outs and TP hits.
It did not count **TIME exits**, which is where the wide-stop trades go (67.0%), nor
the per-loss size under the wider stop.

---

## 4. Proposed change

**Bound the initial stop distance to approximately 1.0–1.2% on all entry paths, and
remove the 2% fallback.** Concretely: make the stop source uniform, and treat a stop
distance above a fixed ceiling as a reason **not to take the trade** rather than a
reason to widen the stop.

**Explicitly rejected: widening the stop further.** The whole finding is that wider is
worse once timeouts and per-loss size are counted. **If the 1.0–1.2% stop produces more
stop-outs, that is acceptable — the tight regime's stop-outs cost −0.024 each and some
recover green.**

**Not in scope:** the TP multiplier, the ratchet parameters, position sizing, the
`imported` lane's aggregate loss, and the risk engine.

---

## 5. What this does NOT establish

1. **The stop regime is confounded with the entry path.** `_paper_open` uses 2% *and*
   is a different strategy from `_pullback_open`/`_paper_loose_open` at 1.2%. **The
   comparison above is therefore stop-and-strategy, not stop alone.** Isolating the stop
   requires the same strategy at both widths — **not done, and it is the first required
   test.**
2. **Paper only.** No live confirmation. The live book is 30 trades.
3. **Costs not modelled.** The 0.28% round-trip assumption is unverified here.
4. **Post-hoc.** These cuts were chosen after seeing outcomes. **The RR proposal's
   §6 falsification tests apply to those cuts and passed; they do NOT yet validate
   the stop-width cut.**

---

## 6. Required before actionable

```
1. Same-strategy stop A/B     the decisive test. One path, both widths, same period.
                              Without this, §5.1 stands and nothing is proven.
2. Re-run the falsification   the RR proposal's §6 four criteria, applied to stop width,
                              with the plan committed BEFORE running.
3. Confirm the 1.0-1.2% band  is 1.2% right, or is 1.0% better? The original comment
                              preferred 2.0 over 1.0 — do not assume 1.2 is optimal.
4. Check the ratchet link     H3 says the ratchet needs a tight stop to fire. Verify
                              directly rather than by inference from exit reasons.
5. Forward paper              fixed period, before any live consideration.
```

---

## 7. Relation to the RR proposal

**The RR floor is not withdrawn as an observation — it is explained.**

```
RR>=2 / SL <1.5%      n=653   win 83.6%   net +55.128
RR>=2 / SL 1.5-2.5%   n= 60   win 18.3%   net  −4.151      <- even high-RR trades lose
```

**If the RR floor had been implemented as proposed, the 60 wide-stop RR≥2 trades would
have been kept and would have continued to lose.** The RR filter would have removed most
of the damage while leaving the mechanism intact — **a proxy fix that looks like a fix.**

**This is the argument for running validation before applying a finding**, and it is the
second time this week that a plausible order-path change did not survive contact with
the data (the first being the 2026-10-04 futures stop/trail edit, reverted the same
night).
