# Overnight shift 2026-10-31 18:00Z — learning pass

## What was checked
- Candle store: 10 symbols, all series current (lags 0.2–0.3h), 0 future rows.
- Lesson loop: 3,672 lessons = 3,472 unified + 200 ensemble. Nothing new (already current to 10:16).
- Unified paper book: 3,472 closed, 0 open.
- Short regime lane: 8 open, 0 closed, gate open 100% of runs.
- Ensemble book: 200 closed, 0 open, last close 2026-10-22.

## Finding 1 — the ensemble book is FROZEN by its own geometry gate, and that is correct
`ensemble-paper-5m` runs every 5 min, `last_status: ok`, and the book has not
closed a trade since 2026-10-22 (9 days). Not a stall: the `margin_pct`
reachability gate refuses every attempt — 2,556 refusals in the log, only two
distinct messages:

    GEOMETRY REFUSED: ens|momentum|5m  target +3.0% — P(reach) below 5%
    GEOMETRY REFUSED: ens|momentum|30m target +3.0% — ceiling 1.5%

So the 2026-10-21 geometry fix worked and its consequence is that this lane
generates no forward record. The instrument warning in the rollup (200 closed,
0 TP, targets 3%/6%, max realised move 2.27%) is therefore no longer a live
data-quality problem — it is a **closed book with a known, unfixed geometry**.
Escalate from "flag it" to "decide it": either retune the ensemble members to a
reachable target (and restart the forward clock at n=0), or retire the lane.
Do NOT leave it running: it consumes a 5-minute cron slot and produces nothing.

## Finding 2 — unified book, 7d cohort: negative, but inside noise
Honest cohort (`pnl_net` present, cost > 0.0015, actual charged cost ≈0.156% RT):

    all-time  n=655  mean -0.00195  t=-0.56  CI [-0.0087,+0.0048]   (not sig)
    last 7d   n=218  mean -0.01285  t=-1.96  CI [-0.0257,-0.00002]  (marginal)
    earlier   n=437  mean +0.00349  t=+0.87                         (not sig)

The sign flip between halves is NOT a finding — both legs are inside noise, and
the 7d leg's t=-1.96 sits exactly on the boundary. Quote it as
"no measured edge, now trending negative on a 218-trade sample", never as
"the strategy turned negative".

### 7d is one day, and that day is the market
    2026-10-29  n=95  -5.3322   <- 56 SLs across 11 symbols
    2026-10-30  n=28  +1.6460
    net ex-best-day  -4.4477

10-29's SLs cluster in three 30-min windows (12, 10, 10) across 11 distinct
symbols — a market flush, not a strategy failure. This is the
`loss-attribution-time-clustering` shape: clustered SLs = the market. The
cohort split tells you *what* lost; the time shape tells you the strategy
did not decide it.

## Finding 3 — cost is being charged at the MEASURED rate, not the doc's 0.30%
Charged cost per $5 trade is 0.0075–0.0078 = **0.15–0.16% round trip**, not the
0.30% that several older notes assume. The same 7d cohort at 0.30% would read
-4.3707 instead of -2.8017. Any historical comparison that mixes the two cost
regimes is comparing fees, not strategy.

## Finding 4 — do not read a 5-trade symbol as a bad symbol
Today OP/USDT was 5 of 16 closes for -0.0985 (all of a +0.0556 day). OP
lifetime is n=346, **net +1.2047**, and +0.0459 over the last 30d. n=5 is
noise; the symbol is fine. First read was wrong, lifetime corrected it.

## Positive control run (lane liveness)
`settle()` in `short_regime_paper.py` was proven alive by injecting a position
anchored 130 bars back: it closed 1, entry 82589.17 -> exit 78235.15,
pnl_gross +0.263595, pnl_net +0.248595 — exact short arithmetic. The 8 open
positions age 43.5 of 120 bars (4h), so `closed: 0` is a genuine
mid-horizon state, not a dead predicate.

## State summary
    data            OK  10/10 series current, 0 future rows
    lesson loop     OK  3,672 lessons, none pending
    unified paper   -   3,472 closed, 0 open, 7d net -2.80 (noise)
    short lane      OK  8 open @43.5/120 bars, gate open 100%
    ensemble        <-  FROZEN by geometry gate since 10-22, needs a decision
    real spot       --  40 closed, 0 open, last close 2026-10-22, armed
