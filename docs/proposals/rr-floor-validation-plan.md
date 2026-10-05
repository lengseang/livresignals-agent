# Split-sample validation plan — RR floor

**Written BEFORE running any test. Parameters fixed here, not adjusted after seeing results.**
If a parameter has to change, the change is recorded as a deviation with its reason.

## The hypothesis under test

> Trades whose constructed reward:risk (RR) is >= 2.0 have a higher expectancy than trades
> with RR < 2.0, and this is NOT an artifact of the specific period or symbol mix that
> produced the original split.

**Falsification criteria (declared now, pass/fail):**

- **V1 fail** if RR>=2 expectancy <= RR<2 expectancy in the out-of-sample half.
- **V2 fail** if the sign flips for either half when split by time.
- **V3 fail** if a single symbol accounts for > 50% of the RR>=2 net in either half.
- **V4 fail** if removing the 3 best RR>=2 trades in the OOS half makes it negative
  (i.e. the effect rests on a handful of outliers).

## Data and method

```
Source        /opt/trading-agent/data/unified-paper.json (closed, with pnl)
Exclusions    records missing pnl (6); records with no derivable RR (no tp/sl, or zero SL)
RR definition abs(tp-entry)/entry  /  abs(entry-sl)/entry      (unchanged from the proposal)
Cohorts       RR>=2  vs  RR<2                                  (threshold fixed at 2.0)
Split         by ts_utc, FIRST HALF vs SECOND HALF in time order (not random)
```

**Why a time split and not random:** a random split of the same period tests
nothing — both halves share the same market regime. **A time split is the weakest
honest test**, and if the effect fails it, it is dead.

**Why the threshold is not swept:** testing 1.5/1.8/2.0/2.5 and reporting the best
is how you manufacture a result. **One threshold, declared, fixed.**

## Outputs required

For each half, and for each cohort within each half:

```
n, win rate, net, average per trade, PF
```

Plus:

```
per-symbol net for RR>=2, each half      (for V3)
OOS RR>=2 net with the 3 best removed    (for V4)
median holding time per cohort           (does RR<2 exit faster? costs are per-trade)
```

## What a PASS means, and what it does not

**PASS** = the direction survives a time split, is not one symbol, and is not three
outliers. **That justifies building the two variants and backtesting them properly.**

**PASS does NOT mean:** apply it live. Live remains a separate gate. A pass moves this
from "post-hoc observation" to "candidate worth implementing and forward-testing".

**FAIL** = the proposal is withdrawn and the RR split was an artifact. Written up as
such, not quietly dropped.
