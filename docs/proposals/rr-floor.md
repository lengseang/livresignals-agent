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

**Enforce an RR floor of 2.0 on every entry, at the point of order construction.**

Concretely, one of:

- **(a)** raise the TP multiplier at lines 326, 426, 480 to `2.0` (uniform behaviour), or
- **(b)** reject signals whose constructed RR would be < 2.0 (keeps the existing
  exit geometry, takes fewer trades),

or a combination. **The choice between (a) and (b) is not made here** — it changes
which signals are traded, and should be settled by the backtest in §6, not by argument.

**Not in scope:** the `imported` lane's aggregate loss, the exit logic, position
sizing, and the risk engine.

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

## 6. Validation required before this is actionable

```
1. Split-sample          re-derive the RR effect on a held-out period; require the
                         sign and rough magnitude to survive.
2. Per-symbol check      the effect must not be one symbol carrying the whole result.
                         XRP is 15 of 30 live trades — check concentration.
3. Geometry sensitivity  test (a) raise-TP vs (b) reject-signal separately. They are
                         different populations and must not be conflated.
4. Forward paper         run either variant in the paper book for a fixed period
                         before any live consideration.
5. Cost re-check         confirm 0.28% is realistic for the retained cohort's
                         holding time.
```

**If the effect does not survive (1) and (2), this proposal is withdrawn.**

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
