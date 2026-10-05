# Finding: TIME exits — a real but small defect, and the mechanism is a gate

**Status: ANALYSIS — read-only, nothing applied.** This document replaces an earlier
draft of itself. **The first version claimed the TIME pool was a fee artefact. That was
wrong and is corrected in §2.**

## 1. The observation

Native paper trades since 2026-09-15 (n=1,555, net +23.616), by exit reason:

```
TP     221  net +44.838  avg +0.20289
STALL  176  net +11.226  avg +0.06378
SL     626  net −24.287  avg −0.03880
TIME   532  net  −8.160  avg −0.01534
```

`TIME` is 34% of exits. `STALL` is the lock-half ratchet banking a gain; **`TIME` is the
clock expiring.**

## 2. CORRECTION — there is no fee artefact

An earlier version of this analysis subtracted an assumed 0.28% round-trip cost and
concluded the pool was "cost, not loss" (−7.45 of −8.16).

**That was wrong. The paper book does not apply transaction costs when writing `pnl`.**
Verified two ways:

```
paper records with exit_px (n=991):
  implied fee  median −0.0001%   mean −0.0000%

live trades, real fills (n=30):
  recovered fees  +0.027%, −0.001%, +0.059%   — noise around zero
```

**`COST_ROUND_TRIP = 0.0028` is a constant in the replay script and appears in code
comments, but the book's recorded `pnl` is gross.** The real fees on actual fills are
around **0.03%**, not 0.28%.

**So the −8.160 is real PnL, and the TIME trades genuinely lose.** The correction makes
the case *harder*, not softer.

## 3. What the pool actually is

```
TIME exits    n=532   win 38.5%   net −8.160

at exit:
  −0.5% .. 0%     144  (27.1%)
   0% .. +0.5%    205  (38.5%)
   0.5% .. 1%       0   (0.0%)

best favourable move:  median +0.334%   mean +0.413%
  reached +0.5%  22.1%     +1.0%  6.5%     +1.5%  0.8%

TP median distance 3.000%      SL median distance 2.000%
```

**Target 3% away, median travel 0.334%.** The trade reaches neither its target nor its
stop and expires near entry, slightly red more often than slightly green.

## 4. THE MECHANISM — a gate that locks out the worst trades

`unified_trader.py:247`:

```python
if pnl_pct >= 0.3 and best > p["entry"] * 1.003:
    ... -> STALL exit (frees the slot after 90 min without a new high)
```

**The stall exit requires the trade to have gone GREEN by 0.3% first.** Measured:

```
TIME trades that never reached +0.3%:   178 trades   net −2.904   avg −0.01631
TIME trades that did pass +0.3%:        221 trades   net +0.179   avg +0.00081
```

**The 178 that never went green cannot satisfy the gate by construction, so they can
never take the early exit and always run the full hold to expiry.** The 221 that did go
green were unaffected.

**So the defect is specific: there is an exit for "went up, then stalled" and none for
"never went anywhere at all."**

## 5. Honest size of the opportunity

```
178 trades × −0.01631 = −2.904   over three weeks, on a 1,555-trade book
```

**~−2.9, not −8.16.** The remainder of the TIME pool is trades that went slightly green
and are effectively flat. **This is a real, mechanical, small defect — not a major leak.**

**For scale: the book earned +23.6 over the same period. Fixing this entirely would move
that by ~12%.**

## 6. Not proposed here

**Whether to change the gate is an order-path question and is not decided.** Options if
pursued — a flat-trade exit that does not require the trade to have gone green first, or
a tighter hold for trades that never move. **Both need forward testing, neither is
proposed as a change.**

**Also unresolved and worth noting:** `exit-params.json` calibrates `hold_hours = 2.0`,
while the code fallback comment says 6h. **If the effective hold is 2h, a flat trade has
only 2h to resolve — and the 178 never-green trades are being held 2h to collect −0.016.**
Confirming which hold is actually in force is a prerequisite for any change here.
