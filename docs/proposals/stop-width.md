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

---

## 8. RESOLUTION ATTEMPT — replay sweep, 2026-10-05. INCONCLUSIVE, and here is why.

Plan: `docs/proposals/stop-width-sweep-plan.md` (written before running).

### 8.1 The naive approach failed first

`unified-paper.json`'s `best_px` field cannot test a stop. It is the **favourable**
extreme — **0 of 1,592 values are below entry** — and it is present on only 68% of
trades, and those are the **winners**:

```
usable (best_px present)  n=1592  win 58.0%  net +49.920
excluded                  n= 746  win 25.9%  net −27.473
```

**The field is recorded when a trade did well and absent when it did not.** Any sweep
on it would measure survivors.

### 8.2 The real replay — against actual candles

`data/trading.db` has 9.5M candles with true `high`/`low`. `scripts/replay_stop_sweep.py`
walks each trade forward bar-by-bar at seven stop widths, 1,347 trades replayable.

```
width     n    win%      net        avg      SL%    TP%   TIME%
 0.8%  1347   39.0%    +1.048   +0.00078   35.2%  10.3%  54.5%
 1.0%  1347   39.9%    -0.687   -0.00051   29.8%  10.8%  59.3%
 1.2%  1347   40.9%    -2.312   -0.00172   25.7%  11.1%  63.3%
 1.5%  1347   41.6%    -5.082   -0.00377   21.6%  11.3%  67.1%
 2.0%  1347   42.4%    -7.586   -0.00563   14.9%  11.6%  73.5%
 2.5%  1347   42.8%   -10.135   -0.00752   11.1%  11.7%  77.1%
 3.0%  1347   43.0%   -12.247   -0.00909    8.0%  11.7%  80.3%
```

**The ordering supports the proposal — monotone, no plateau, tighter is better at every
step. The SL%/TIME% columns reproduce the substitution mechanism exactly (stop-outs
35%→8% as timeouts 54%→80%). And TP% is nearly flat (10.3%→11.7%), which removes the
original rationale for widening: a wider stop does NOT reach target more often.**

### 8.3 But the absolute numbers are wrong, so the ordering is all we have

**Every width loses in the replay; the book is +22 net.** The replay has **no lock-half
ratchet**, and the book's `STALL` exits — the ratchet firing — average **+0.058/trade
across 237 trades** and are a large part of the book's profit. **The replay cannot
reproduce the mechanism that generates the profit, so its levels are meaningless and only
its ordering is usable.**

### 8.4 The calibration disagreement is now explained

`exit-params.json` swept the same question on **30 real trades** and picked 2.5%.
Between 1.0% (+0.00094) and 2.5% (+0.00270) the difference is **0.0018 USDT per trade**
— indistinguishable from noise on n=30. **The "plateau at 2.5–5.0%" is the sweep running
out of data, not a real optimum.** It selected the flat end of a curve on far too few
observations.

### 8.5 Verdict: NEITHER ESTABLISHED — proposal not actionable

```
my tight-stop reading      ordering supported, levels wrong (no ratchet in replay)
the calibration's 2.5%     n=30, differences within noise
the original code comment  counted stop-outs and TP, never TIME exits
```

**Do NOT change the stop width on this evidence.** The correct next step is not another
analysis — it is to **fix the recording gap**:

```
add `worst_px` (intra-trade adverse extreme) to the paper book's closed records
```

**One field. Then this question is directly answerable instead of inferred.** Until it
exists, every stop-width result is a reconstruction with a known hole in it.

**The proposal is retained as an open question, not a recommendation.**
