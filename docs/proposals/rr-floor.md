# Proposal: enforce an RR≥2 floor on entries

> **Status: PROPOSAL — not applied, not approved, no order-path change made.**
> Evidence gathered 2026-10-05 from the existing paper and live records. Read-only work.
> Proposal only: nothing here activates itself. Per the standing rule, strategy changes
> go through branch → tests → backtest → validation → paper → promotion.
>
> **Decides nothing on its own.** If approved, the implementation still has to be
> validated on a held-out sample before it touches a live path.

---

## 1. The finding

Every trade carries a reward:risk ratio, set by the TP/SL multipliers at the call site
that created it. In both the paper book and the live account, **the RR of the entry
predicts the outcome far more strongly than any other single variable measured.**

### Paper book — `data/unified-paper.json`, 2,334 closed trades

| Cohort | n | Win rate | Net | Avg/trade |
|---|---|---|---|---|
| **RR ≥ 2** | **725** | **77.1%** | **+50.03** | **+0.0690** |
| **RR < 2** | **973** | **26.6%** | **−36.18** | **−0.0372** |
| (no RR derivable) | 639 | — | +8.41 | — |

**Split by lane** (the two open paths, which differ only in their TP/SL multipliers):

| Lane | RR | n | Win | Net | Avg |
|---|---|---|---|---|---|
| loose | ≥2 | 371 | 93.0% | +40.958 | +0.11040 |
| loose | <2 | 616 | 22.9% | −28.119 | −0.04565 |
| tight | ≥2 | 351 | 60.4% | +8.962 | +0.02553 |
| tight | <2 | 357 | 33.1% | −8.058 | −0.02257 |

### Live account — `data/live-trades.json`, 30 closed trades

| Cohort | n | Win | Net |
|---|---|---|---|
| RR ≥ 2 | 11 | 63.6% | **+0.2011** |
| RR < 2 | 16 | 31.3% | **−0.4817** |

**The live account shows the same direction** — RR≥2 positive, RR<2 negative — on a
sample far too small to be conclusive on its own, but consistent with paper.

---

## 2. Why this is not a lane problem

An earlier hypothesis was that the `loose` lane was the drag. **The data says the
opposite:**

| Lane | n | Win | Net | Avg | PF |
|---|---|---|---|---|---|
| loose | 1,309 | 48.7% | +18.370 | **+0.01403** | **1.48** |
| tight | 1,025 | 46.4% | +3.786 | +0.00369 | 1.14 |
| imported | 340 | 40.0% | **−2.087** | −0.00614 | **0.78** |

**`loose` is the most profitable lane by average PnL.** **The RR effect is present
inside *both* lanes, at similar magnitude.** So the correct lever is the RR of the
entry, not which lane produced it.

**`imported` is the only lane that loses money overall**, and it is small (n=340).
It is a separate question from this proposal; noted, not addressed here.

---

## 3. The mechanism

`scripts/unified_trader.py` sets TP/SL at four call sites, each with a hardcoded
multiplier. The RR is therefore a property of the *code path*, not a filter applied to
the signal:

| Line | Function | TP multiplier | RR |
|---|---|---|---|
| 181 | `_paper_open` | `stop * 2` | 2.0 |
| 326 | `_pullback_open` | `stop * 1.5` | 1.5 |
| 426 | `_paper_loose_open` | `stop * 1.5` | 1.5 |
| 480 | `_imported_strategy_open` | `stop * 1.8` | 1.8 |

**There is no RR filter anywhere in the entry path.** Trades with RR<2 are not
"slipping through" a gate — they are deliberately created by three of the four call
sites. Removing RR<2 trades is therefore a change to what those sites emit, not the
addition of a guard to a pipeline.

---

## 4. Proposed change

**Enforce an RR floor of 2.0 on every entry, by REJECTING the signal.**

**This section originally offered two options — (a) raise the TP multiplier, or (b) reject
the signal. The validation run (§6.1) settles it in favour of (b), and against (a):**

The exit mix explains why. The RR<2 cohort does not lose by stopping out — it loses by
*stalling*:

```
RR>=2   TP 31.0%   SL 44.6%   TIME 10.9%   STALL 13.5%
RR<2    TP  6.1%   SL 34.4%   TIME 58.0%   STALL  1.3%
```

**58% of RR<2 trades exit on TIME, and only 6.1% reach target.** These are entries that
drift sideways until the clock closes them. **Widening the target (option a) would make
that strictly worse — a trade reaching a 1.5R target 6% of the time will reach a 2R
target less often still.** The defect is in the *entry*, so the fix is to not take it.

Concretely: at the point of order construction, compute the RR the trade would have and
**skip the signal if it is < 2.0**.

**Not in scope:** the `imported` lane's aggregate loss, the exit logic, position sizing,
and the risk engine.

**Open question this raises (not answered here):** *why* do these entries stall? Filtering
by RR removes the symptom. If a signal fires and 58% of the time goes nowhere, the
signal generator — not the exit geometry — is where the cause lives. **Finding that is
worth more than the filter.**

---

## 5. Expected effect, stated honestly

**If** the relationship holds out of sample, removing RR<2 entries changes the paper
book from:

```
all trades     n=2,337   net +22.264   avg +0.00953
RR>=2 only     n=  725   net +50.03    avg +0.0690
```

**That is a large claimed improvement (+27.8 net on the same period, 7.2× the average
per trade) and it should be treated with suspicion until validated.** Section 6 exists
for that reason.

**Risks the proposal does not dismiss:**

1. **Selection on the outcome we're measuring.** RR and result are both derived from
   the same TP/SL geometry. A high-RR trade that wins did so *because* its TP was far
   and it got there. This is not automatically circular, but it is close enough that
   the split-sample test is mandatory.
2. **The win-rate gap is implausibly large.** 93% at RR≥2 versus 23% at RR<2 in the
   same lane is a 70-point spread. **A gap that size usually means the low-RR entries
   are low-quality signals, not merely differently-shaped exits.** If so, an RR floor
   fixes the *symptom*; the *cause* is in signal selection.
3. **Costs are not modelled.** Paper assumes 0.28% round-trip. If RR<2 trades are
   shorter-duration, their real cost is proportionally higher, which would make the
   measured effect larger, not smaller — but it also means the paper numbers for the
   retained cohort may be optimistic.
4. **n=30 live trades cannot confirm anything.** The live direction agrees; it carries
   almost no weight.

---

## 6. Validation — RUN 2026-10-05, RESULT: PASS (4/4)

Plan written before running, at `docs/proposals/rr-floor-validation-plan.md`. Method:
time-ordered split (not random), one threshold (2.0), no sweeping. **Falsification
criteria were declared in advance and all four were checked.**

```
usable trades  1,698 of 2,337 with a derivable RR
H1             2026-08-29 .. 2026-09-20   (849)
H2             2026-09-20 .. 2026-10-05   (849)
```

| Test | Criterion | Result |
|---|---|---|
| **V1** | RR>=2 avg > RR<2 avg in the OOS half | **PASS** — H1 +0.06244 vs −0.02998; H2 +0.07499 vs −0.04488 |
| **V2** | sign holds in both halves | **PASS** — same direction, magnitude *stronger* in H2 |
| **V3** | no single symbol > 50% of RR>=2 net | **PASS** — max share 25% (H1), 26% (H2) |
| **V4** | OOS RR>=2 survives removing its 3 best | **PASS** — +28.422 → +27.353 |

Full figures:

```
H1  RR>=2  n=346  win=73.1%  net=+21.605  avg=+0.06244  PF=4.65
H1  RR<2   n=503  win=30.8%  net=−15.081  avg=−0.02998  PF=0.37
H2  RR>=2  n=379  win=80.7%  net=+28.422  avg=+0.07499  PF=7.21
H2  RR<2   n=470  win=22.1%  net=−21.096  avg=−0.04488  PF=0.09
```

### 6.1 What the validation changed

It settled §4 in favour of **rejecting** rather than **re-widening**, via the exit mix
(see §4). It did **not** change the direction of the finding.

**Remaining validation before this is actionable** (unchanged from the plan):

```
1. split-sample            DONE — pass
2. per-symbol concentration DONE — pass
3. geometry: (a) vs (b)    RESOLVED by the exit mix — (b)
4. forward paper           NOT DONE — build variant (b), run it in paper
5. cost re-check           NOT DONE — RR<2 exits are TIME-heavy; confirm 0.28% holds
```

**Steps 4 and 5 are the gate to live. Neither is done.**

---

## 7. What is deliberately NOT claimed

- Not claimed: that the edge is proven. **n is large in paper but the split is
  post-hoc**, which is the classic way to find a relationship that does not generalise.
- Not claimed: that this should be applied to live. **Live is a separate gate.**
- Not claimed: that RR<2 trades are worthless. **They are 973 observations that funded
  a finding — they are evidence, not waste.**

---

## 8. Data sources

```
/opt/trading-agent/data/unified-paper.json   2,340 closed (2,334 with pnl)
/opt/trading-agent/data/live-trades.json     30 closed (real fills)
/opt/trading-agent/scripts/unified_trader.py TP/SL call sites, lines 181/326/426/480
```

All figures above were computed read-only from these files. **The 6 paper records
lacking a `pnl` field were excluded** (they are `imported` entries closed on `TIME`
with no recorded result). The 639 records with no derivable RR either lack `tp`/`sl`
or have a zero SL distance; they are reported as a third row and not folded into
either cohort.
