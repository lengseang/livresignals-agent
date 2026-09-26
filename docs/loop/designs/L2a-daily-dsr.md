# L2a · Pre-registration: the gate's Deflated Sharpe on daily returns

Written and committed **before** any daily-return DSR was computed on any run.

## Why change the basis
The gate's DSR has been computed on **OOS per-trade net returns** (T3.3), because runs didn't save
their equity curve. Bailey & López de Prado (2014) define the Sharpe ratio on a *return series*
sampled at a fixed frequency. Per-trade returns have an irregular spacing (a trend trade lasts days
to months), a small T (133–354 for trend_real), and extreme fat tails (skew 9–12, kurtosis 90–159),
all of which hit the PSR denominator. Daily returns of the shared account are the standard basis,
and they also capture the risk while a position is open, which per-trade returns ignore.

## The rule (fixed now, applied to every run the same way)
1. Every replay saves `equity_daily.json`: the last mark of each UTC day for each account (net,
   gross, stress, and benchmarks), the same sampling `metrics.daily_sharpe` already uses.
2. DSR observations = **OOS daily net returns** of the `net` account, over the run's OOS window.
   SR is the non-annualised daily Sharpe; skew and kurtosis are taken from the same series.
3. **Consistency:** V[SR_n] uses each distinct trial's OOS daily Sharpe. If any trial in N lacks a
   daily curve, the daily DSR is **not computed** and the verdict is INSUFFICIENT_DATA until that
   trial is re-run. Bases are never mixed.
4. N is unchanged: distinct (strategy, params, symbols, trade window), as in T3.3b. Re-runs that
   add a daily curve are the same trials.
5. The threshold stays **0.95**. The other gate checks are unchanged (shared-account net > 0,
   OOS net > 0, mean net per trade > 0 or win rate > p*, ≥ 30 OOS trades, regime stated).
6. The per-trade DSR stays in `gate.json` as a secondary, reported number. The verdict uses the daily DSR.
7. **Accepted either way.** If a strategy that failed per-trade passes daily, it is a candidate for
   the one-time final test (L2c), not a result. If one that looked close fails, that stands.

## What would make this change suspect
Changing the basis after seeing results can be p-hacking. The safeguards are: this document
predates every daily-DSR number; the rule is the paper's own standard rather than a new invention;
it applies to all 28 trials at once; and a PASS under it still has to survive the out-of-universe
coins and the untouched holdout.

## Clarification (2026-09-26, while implementing; before any daily DSR was computed on a real run)
Runs made before L2a have no `equity_daily.json`. They keep the per-trade DSR, labelled
`dsr_basis: legacy` in `gate.json`, until they are re-run. Every one of the 28 logged trials is
re-run under L2a, so all real verdicts from here on use the daily basis.
