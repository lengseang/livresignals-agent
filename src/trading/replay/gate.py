"""The validation gate every strategy must pass (T3.3). Research only; the verdict is about a REPLAY.

    python -m trading.replay.gate --run data/local/replay/<run_id>

Reads `<run>/summary.json`, `<run>/events.jsonl` and the trial log `<run>/../trials.jsonl`, and writes
`<run>/gate.json` and `<run>/gate.md`. Nothing is re-run or re-tuned, and the holdout is never touched.

Deflated Sharpe Ratio (Bailey & López de Prado, "The Deflated Sharpe Ratio: Correcting for Selection Bias,
Backtest Overfitting and Non-Normality", J. Portfolio Management 40(5), 2014; SSRN 2460551, eq. 2):

    DSR = PSR(SR0) = Z[ (SR - SR0) * sqrt(T - 1) / sqrt(1 - g3*SR + (g4 - 1)/4 * SR^2) ]

    SR0 = sqrt(V[SR_n]) * ( (1 - gamma) * Zinv[1 - 1/N] + gamma * Zinv[1 - 1/(N e)] )     (eq. 1)

where SR is the selected strategy's non-annualised Sharpe ratio over T observations, g3 its skewness,
g4 its (raw, non-excess) kurtosis, N the number of independent trials, V[SR_n] the variance of the trials'
Sharpe ratios (same units as SR), gamma ~ 0.5772 the Euler-Mascheroni constant, Z the standard Normal CDF.
With N = 1 there is no selection, SR0 = 0 and DSR is the plain PSR against zero. Verified against the
published worked example (SR0 = 0.1132, DSR = 0.9004), see tests/loop/test_gate.py.

Here the observations are the strategy's **out-of-sample closed trades** (net per-trade returns of the
shared "net" account), because the replay does not persist its equity curve.

What counts as a trial (T3.3b). N is the number of DISTINCT hypotheses in the trial log, not the number of
logged runs. A hypothesis is `trial_key()`: strategy + params + universe (symbols, order-free) + trade window.
Re-running the same hypothesis on a different cost model or broker (flat costs vs the measured paper fill
model), or with a new run_id, is the same trial: it re-prices the same selection, it does not add one. A
different universe or window IS a new trial (it is a researcher choice, and counting it keeps N
conservative). Every variant that was looked at and discarded still counts. The raw log is never rewritten;
the grouping is recomputed on each gate run and written into gate.json (checks.dsr.trials). Each distinct
trial contributes one Sharpe to V[SR_n]: the gated run's own for its group, otherwise the latest logged run
of that group whose Sharpe is recoverable.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.stats import norm

PASS, FAIL, INSUFFICIENT = "PASS", "FAIL", "INSUFFICIENT_DATA"
LABEL = "REPLAY (simulated; research, not evidence of edge)"

EULER_GAMMA = 0.5772156649015329
DSR_THRESHOLD = 0.95
# Never PASS on fewer than 30 out-of-sample trades. PSR/DSR rest on the asymptotic Normality of the Sharpe
# estimator (Lo 2002; Mertens 2002) and use the sample skewness and kurtosis, which are very unstable in
# small samples; n >= 30 is the conventional minimum for a CLT approximation. It is a floor, not a
# sufficiency claim: with fat tails far more trades are needed, which the DSR itself then reflects.
MIN_OOS_TRADES = 30
# Pre-registered IS/OOS split of the baseline (docs/loop/designs/T3.0-T3.1.md), used only when the run's
# summary predates run.py recording it.
DEFAULT_IS_OOS_SPLIT = "2022-01"


# ----- statistics --------------------------------------------------------------------------------
def expected_max_sharpe(var_trial_sr: float | None, n_trials: int) -> float:
    """E[max SR_n] under the null of zero skill, Bailey & López de Prado 2014 eq. 1 (mean of trial SRs = 0)."""
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if n_trials == 1:
        return 0.0
    if var_trial_sr is None or var_trial_sr < 0:
        raise ValueError("variance of trial Sharpe ratios is required when n_trials > 1")
    z = (1 - EULER_GAMMA) * norm.ppf(1 - 1 / n_trials) + EULER_GAMMA * norm.ppf(1 - 1 / (n_trials * math.e))
    return math.sqrt(var_trial_sr) * float(z)


def psr(sr: float, t: int, skew: float, kurt: float, sr_star: float) -> float:
    """Probabilistic Sharpe Ratio: P(true SR > sr_star). kurt is raw (Normal = 3)."""
    if t < 2:
        raise ValueError("need at least 2 observations")
    var_term = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if not var_term > 0:
        raise ValueError(f"PSR undefined: 1 - skew*SR + (kurt-1)/4*SR^2 = {var_term:.4g} is not positive")
    return float(norm.cdf((sr - sr_star) * math.sqrt(t - 1) / math.sqrt(var_term)))


def critical_sr0(sr: float, t: int, skew: float, kurt: float, threshold: float = DSR_THRESHOLD) -> float:
    """Largest benchmark SR0 at which PSR(SR0) still reaches `threshold`: how much selection the result absorbs."""
    var_term = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if not var_term > 0 or t < 2:
        raise ValueError("PSR undefined for these moments")
    return sr - float(norm.ppf(threshold)) * math.sqrt(var_term) / math.sqrt(t - 1)


def deflated_sharpe(sr: float, t: int, skew: float, kurt: float, n_trials: int,
                    var_trial_sr: float | None) -> float:
    return psr(sr, t, skew, kurt, expected_max_sharpe(var_trial_sr, n_trials))


def sharpe_moments(returns) -> dict:
    """Non-annualised SR (sample std, ddof=1), skewness and raw kurtosis (population moments)."""
    x = np.asarray(returns, dtype=float)
    n = len(x)
    if n < 2:
        return {"n": n, "sr": None, "skew": None, "kurtosis": None}
    mu, sd = float(x.mean()), float(x.std(ddof=1))
    d = x - mu
    m2 = float(np.mean(d ** 2))
    skew = float(np.mean(d ** 3) / m2 ** 1.5) if m2 > 0 else None
    kurt = float(np.mean(d ** 4) / m2 ** 2) if m2 > 0 else None
    return {"n": n, "sr": (mu / sd) if sd > 0 else None, "skew": skew, "kurtosis": kurt}


def round_trip_cost(cost_model: dict, symbols=None) -> float:
    """c = 2(f + s), s = half spread + slippage per side (docs/research/fees-and-breakeven.md).

    A legacy flat model (`fee_rate`, `spread_bps`, `slippage_bps`) has one c. A paper-broker fill header
    (`fee_taker`, `spread_stat`) has a measured spread per symbol; c is then the WORST (widest) of the run's
    symbols, a conservative bound. An unmeasured symbol raises (UnknownSpreadError), never guessed.
    """
    if "fee_rate" in cost_model:
        return 2 * (cost_model["fee_rate"] + (cost_model["spread_bps"] / 2 + cost_model["slippage_bps"]) / 1e4)
    from trading.paper import spreads
    stat = cost_model.get("spread_stat", "median")
    if stat == "none":
        return 2 * cost_model["fee_taker"]
    if not symbols:
        raise ValueError("a paper fill header needs the run's symbols to price the spread")
    worst = max(spreads.spread_bp(s, stat, cost_model.get("spread_override")) for s in symbols)
    return 2 * (cost_model["fee_taker"] + worst / 2 / 1e4)


def breakeven_win_rate(tp: float, sl: float, cost: float) -> float:
    """p* = (sl + c) / (tp + sl): expectancy is zero at this win rate (same formula as research/07-breakeven.py)."""
    return (sl + cost) / (tp + sl)


# ----- verdict -----------------------------------------------------------------------------------
def verdict(*, oos_net_trade_returns, oos_gross_trade_returns, full_net_return, oos_net_return,
            oos_btc_net_return, regime, cost_model, n_trials, trial_sharpes=None, exits=None,
            symbols=None, dsr_daily=None, benchmarks=None, target_weeks=None) -> dict:
    fails, short, checks = [], [], {}
    net = [float(r) for r in oos_net_trade_returns]
    gross = [float(r) for r in oos_gross_trade_returns]
    n = len(net)
    target = target_weeks is not None      # P0d: a target-weight run (positions held for years and trimmed)

    if target:
        checks["oos_trades"] = {"basis": "OOS weeks with at least one fill (target-weight run, P0d)",
                                "value": target_weeks, "min": MIN_OOS_TRADES, "ok": target_weeks >= MIN_OOS_TRADES,
                                "closed_trades_in_oos": n}
        if target_weeks < MIN_OOS_TRADES:
            short.append(f"only {target_weeks} OOS weeks with a fill (< {MIN_OOS_TRADES}): too little activity to judge")
    else:
        checks["oos_trades"] = {"value": n, "min": MIN_OOS_TRADES, "ok": n >= MIN_OOS_TRADES}
        if n < MIN_OOS_TRADES:
            short.append(f"only {n} OOS trades (< {MIN_OOS_TRADES}): too few to judge; never a PASS")

    checks["shared_account_net"] = {"value": full_net_return, "ok": full_net_return is not None and full_net_return > 0}
    if full_net_return is None:
        short.append("shared-account net result is UNKNOWN")
    elif full_net_return <= 0:
        fails.append(f"shared-account net result after costs {full_net_return:+.2%} is not positive")

    checks["oos_net"] = {"value": oos_net_return, "ok": oos_net_return is not None and oos_net_return > 0}
    if oos_net_return is None:
        short.append("OOS net return is UNKNOWN")
    elif oos_net_return <= 0:
        fails.append(f"OOS net return {oos_net_return:+.2%} is not positive")

    beats = None if oos_net_return is None or oos_btc_net_return is None else oos_net_return > oos_btc_net_return
    checks["btc_benchmark"] = {"strategy_oos_net": oos_net_return, "btc_buy_hold_oos_net": oos_btc_net_return,
                               "beats_btc": beats, "gating": False,
                               "note": "reported and flagged, not a pass/fail criterion (strategy-audit.md rule 3)"}

    label = (regime or {}).get("label")
    checks["regime"] = {"label": label or "UNKNOWN", "detail": regime, "ok": bool(label) and label != "UNKNOWN"}
    if not checks["regime"]["ok"]:
        short.append("regime of the OOS window is UNKNOWN")

    c = round_trip_cost(cost_model, symbols)
    basis = ("flat cost model" if "fee_rate" in cost_model else
             f"paper fill model: taker fee + the worst {cost_model.get('spread_stat', 'median')} measured spread "
             f"among the run's symbols (conservative)")
    mean_net = float(np.mean(net)) if net else None
    mean_gross = float(np.mean(gross)) if gross else None
    win = (sum(r > 0 for r in net) / n) if n else None
    econ = {"round_trip_cost": c, "cost_basis": basis, "mean_net_return_per_trade": mean_net, "mean_gross_return_per_trade": mean_gross,
            "win_rate_net": win}
    if exits:
        p_star = breakeven_win_rate(exits["tp"], exits["sl"], c)
        econ.update(mode="fixed_exits", tp=exits["tp"], sl=exits["sl"], breakeven_win_rate=p_star,
                    ok=win is not None and win > p_star)
        if win is not None and win <= p_star:
            fails.append(f"win rate {win:.1%} is not above its break-even p* {p_star:.1%}")
    else:
        econ.update(mode="mean_net_vs_cost", ok=mean_net is not None and mean_net > 0,
                    gross_mean_over_cost=(mean_gross / c) if (mean_gross is not None and c > 0) else None,
                    note="exits are not fixed TP/SL, so p* does not apply; mean net return per trade is compared "
                         "with zero and the mean gross move with the round-trip cost")
    if target:
        econ.update(mode="not_applicable", ok=None,
                    note="P0d: target-weight positions are trimmed, not round-tripped, so per-trade economics do not "
                         "describe them; the verdict rests on OOS net return, the daily DSR and the benchmarks")
    elif mean_net is None:
        short.append("no OOS trades, per-trade economics UNKNOWN")
    elif not exits and mean_net <= 0:
        fails.append(f"mean net return per trade {mean_net:+.3%} is not positive "
                     f"(mean gross {mean_gross:+.3%} vs round-trip cost {c:.3%})")
    checks["trade_economics"] = econ

    fails_before_dsr, short_before_dsr = len(fails), len(short)
    mom = sharpe_moments(net)
    trial_sr = [s for s in (trial_sharpes or []) if s is not None]
    var = float(np.var(trial_sr, ddof=1)) if len(trial_sr) >= 2 else None
    d = {"threshold": DSR_THRESHOLD, "n_trials": n_trials, "trial_sharpes_found": len(trial_sr),
         "var_trial_sr": var, "sr_per_trade": mom["sr"], "t": mom["n"], "skew": mom["skew"],
         "kurtosis": mom["kurtosis"], "sr0": None, "value": None, "ok": False,
         "observations": "OOS closed trades, net per-trade returns"}
    if n < MIN_OOS_TRADES:
        d["why_not_computed"] = f"fewer than {MIN_OOS_TRADES} OOS trades"
    elif mom["sr"] is None or mom["skew"] is None:
        d["why_not_computed"] = "per-trade returns have zero variance"
        short.append("DSR not computable: per-trade returns have zero variance")
    elif n_trials > 1 and var is None:
        d["why_not_computed"] = f"{n_trials} trials logged but Sharpe ratios found for only {len(trial_sr)}"
        short.append(f"DSR not computable: {n_trials} trials logged, Sharpe ratios recoverable for {len(trial_sr)} "
                     f"(need >= 2 for V[SR_n])")
    else:
        try:
            d["sr0"] = expected_max_sharpe(var, n_trials)
            d["value"] = psr(mom["sr"], n, mom["skew"], mom["kurtosis"], d["sr0"])
            d["ok"] = d["value"] >= DSR_THRESHOLD
            d["sr0_critical"] = critical_sr0(mom["sr"], n, mom["skew"], mom["kurtosis"])
            d["sr0_critical_note"] = ("DSR stays >= threshold while E[max SR] of all trials, including unlogged "
                                      "ones, is below this (per-trade units)")
            if not d["ok"]:
                fails.append(f"DSR {d['value']:.4f} < {DSR_THRESHOLD} (N = {n_trials} trials, T = {n} OOS trades)")
        except ValueError as e:
            d["why_not_computed"] = str(e)
            short.append(f"DSR not computable: {e}")
    checks["dsr"] = d
    if dsr_daily is not None:
        # L2a (pre-registered): the verdict uses the DSR on OOS daily net returns; per-trade stays as secondary
        checks["dsr_per_trade"] = d
        del fails[fails_before_dsr:], short[short_before_dsr:]
        d = {**dsr_daily, "threshold": DSR_THRESHOLD}
        if d.get("value") is None:
            short.append(f"daily DSR not computable: {d.get('why_not_computed', 'UNKNOWN')}")
        else:
            d["ok"] = d["value"] >= DSR_THRESHOLD
            if not d["ok"]:
                fails.append(f"daily DSR {d['value']:.4f} < {DSR_THRESHOLD} (N = {d['n_trials']} trials, "
                             f"T = {d['t']} OOS days)")
        checks["dsr"] = d

    if benchmarks is not None:
        # P0b (pre-registered): the OOS daily net Sharpe must beat BTC buy-and-hold AND the equal-weight basket
        checks["benchmarks"] = benchmarks
        if benchmarks.get("ok") is None:
            short.append(f"benchmark check not computable: {benchmarks.get('why_not_computed', 'UNKNOWN')}")
        elif not benchmarks["ok"]:
            fails.append("OOS daily Sharpe {:.2f}/yr does not beat {}".format(
                benchmarks["strategy_sr_ann"], " and ".join(
                    f"{k} {v:.2f}/yr" for k, v in benchmarks["benchmarks_sr_ann"].items()
                    if v is not None and benchmarks["strategy_sr_ann"] <= v)))

    # fail closed: nothing PASSes without a computed DSR (a target-weight run has no per-trade DSR, so it needs the
    # daily one; before P0d the trade-count rule happened to block this, P0d removed that for target runs)
    if checks["dsr"].get("value") is None and not any("DSR" in x for x in short):
        short.append("DSR not computed: " + ("a target-weight run needs the daily DSR (equity_daily.json)" if target
                                             else checks["dsr"].get("why_not_computed", "UNKNOWN")))

    v = FAIL if fails else INSUFFICIENT if short else PASS
    reasons = fails + short if v != PASS else [
        (f"{target_weeks} OOS weeks with a fill >= {MIN_OOS_TRADES}; shared-account and OOS net positive; " if target else
         f"{n} OOS trades >= {MIN_OOS_TRADES}; shared-account and OOS net positive; mean net per trade positive; ") +
        f"{'daily ' if dsr_daily is not None else ''}DSR {d['value']:.4f} >= {DSR_THRESHOLD} with N = {d.get('n_trials', n_trials)}"]
    return {"verdict": v, "reasons": reasons, "checks": checks}


# ----- L2a: DSR on OOS daily returns (docs/loop/designs/L2a-daily-dsr.md) ---------------------------------
def oos_daily_returns(series: list, t0: int, t1: int) -> list[float]:
    """Daily returns in (t0, t1), starting from the last daily mark at or before t0."""
    days = [d for d, _ in series]
    i0 = next((i for i in range(len(days) - 1, -1, -1) if days[i] <= t0), None)
    if i0 is None:
        return []
    vals = [v for d, v in series[i0:] if d < t1]
    return [b / a - 1 for a, b in zip(vals, vals[1:])]


def _daily_curve(run: Path, account: str = "net") -> list | None:
    p = run / "equity_daily.json"
    return json.loads(p.read_text(encoding="utf-8")).get(account) if p.exists() else None


# P0a (docs/loop/designs/P0a-dsr-family.md), pre-registered 2026-09-27 before any P1 result existed.
# Families are fixed by strategy TYPE, never by results. N stays every distinct trial; only the spread of trial
# Sharpes comes from the tested strategy's own family, so an unrelated family that lost everything can't set
# the bar. A strategy not listed here is a family of its own.
FAMILIES = {
    "trend": {"trend_sma", "trend_real", "donchian_ensemble", "tsmom_risk_managed"},
    "cross_section": {"ctrend_lite", "xs_momentum", "high_52w"},
    "intraday": {"intraday_momentum", "intraday_reversal", "btc_lead_lag"},
    "breakout": {"range_breakout"},
    "regime": {"regime_ls"},
    "benchmark": {"buy_hold_btc", "buy_hold_ew"},
}


def family_of(strategy: str | None) -> str:
    for fam, members in FAMILIES.items():
        if strategy in members:
            return fam
    return f"own:{strategy}"


def _strategy_of(run: Path) -> str | None:
    p = run / "summary.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("strategy") if p.exists() else None
    except ValueError:
        return None


BENCHMARK_CURVES = {"btc_hold": "bh_net", "equal_weight_hold": "bh_ew_net"}


def target_weeks(run: Path, t0: int, t1: int, account: str = "net") -> int | None:
    """P0d: None unless the run traded by target weight; else distinct OOS weeks with a fill in `account`."""
    p = run / "events.jsonl"
    if not p.exists():
        return None
    is_target, weeks = False, set()
    with open(p, encoding="utf-8") as f:
        for line in f:
            if '"rebalance"' not in line and '"target"' not in line and '"signal"' not in line and '"close"' not in line:
                continue
            e = json.loads(line)
            pl = e.get("payload") or {}
            if e.get("type") == "rebalance" or (e.get("type") == "strategy_signal" and pl.get("action") == "target"):
                is_target = True
            if e.get("type") in ("signal", "close", "rebalance") and pl.get("account") == account                     and t0 <= e.get("ts_ms", -1) < t1:
                weeks.add(e["ts_ms"] // (7 * 86_400_000))
    return len(weeks) if is_target else None


def benchmark_check(run: Path, t0: int, t1: int) -> dict:
    """P0b: OOS daily net Sharpe of the run vs each benchmark's over the same days. Missing curve -> not computable."""
    p = run / "equity_daily.json"
    curves = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    out = {"basis": "OOS daily net Sharpe, annualised x sqrt(365)", "ok": None,
           "strategy_sr_ann": None, "benchmarks_sr_ann": {}}
    missing = [k for k in ["net", *BENCHMARK_CURVES.values()] if not curves.get(k)]
    if missing:
        out["why_not_computed"] = f"no daily curve {', '.join(missing)} (run made before P0b; re-run it)"
        return out
    ann = math.sqrt(365)
    sr = lambda k: sharpe_moments(oos_daily_returns(curves[k], t0, t1))["sr"]
    s0 = sr("net")
    bench = {name: sr(key) for name, key in BENCHMARK_CURVES.items()}
    if s0 is None or any(v is None for v in bench.values()):
        out["why_not_computed"] = "a daily series has zero variance or too few days"
        return out
    out["strategy_sr_ann"] = s0 * ann
    out["benchmarks_sr_ann"] = {k: v * ann for k, v in bench.items()}
    out["ok"] = all(s0 > v for v in bench.values())
    return out


def daily_dsr(run: Path, trial_runs: list[Path], t0: int, t1: int) -> dict:
    """Deflated Sharpe of `run`'s OOS daily net returns against every distinct trial's OOS daily Sharpe.
    Never mixes bases: a trial without a daily curve makes it not computable.

    N = every distinct trial. The variance of trial Sharpes comes from the trials in `run`'s family (P0a);
    with fewer than 2 family members it falls back to all trials, which is the stricter choice."""
    out = {"basis": "OOS daily net returns", "n_trials": len(trial_runs), "value": None, "sr0": None}
    missing = [r.name for r in trial_runs if _daily_curve(r) is None]
    if missing or _daily_curve(run) is None:
        out["why_not_computed"] = f"trial(s) {', '.join(missing or [run.name])} lacks a daily curve; re-run them"
        return out
    trial_sr = [sharpe_moments(oos_daily_returns(_daily_curve(r), t0, t1))["sr"] for r in trial_runs]
    mom = sharpe_moments(oos_daily_returns(_daily_curve(run), t0, t1))
    out.update(t=mom["n"], sr_daily=mom["sr"], skew=mom["skew"], kurtosis=mom["kurtosis"], trial_sharpes=trial_sr)
    if mom["sr"] is None or mom["skew"] is None or any(x is None for x in trial_sr):
        out["why_not_computed"] = "a daily return series has zero variance or too few days"
        return out
    fam = family_of(_strategy_of(run))
    fam_sr = [s for r, s in zip(trial_runs, trial_sr) if family_of(_strategy_of(r)) == fam]
    pool, basis = (fam_sr, f"family {fam} ({len(fam_sr)} trials)") if len(fam_sr) >= 2 \
        else (trial_sr, f"all trials (family {fam} has {len(fam_sr)})")
    var = float(np.var(pool, ddof=1)) if len(pool) >= 2 else None
    out["var_trial_sr"] = var
    out["var_basis"] = basis
    out["sr0"] = expected_max_sharpe(var, len(trial_runs))
    out["value"] = psr(mom["sr"], mom["n"], mom["skew"], mom["kurtosis"], out["sr0"])
    return out


# ----- reading a run -----------------------------------------------------------------------------
def _ms(iso_or_ym: str) -> int:
    s = iso_or_ym if "T" in iso_or_ym else iso_or_ym + "-01T00:00:00Z"
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() * 1000)


def oos_trades(run: Path, split_ms: int, end_ms: int, account: str = "net") -> list[dict]:
    """Closed trades of `account` opened at or after the split and closed by the window end."""
    opened, out = {}, []
    with open(run / "events.jsonl", encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            p = e.get("payload", {})
            if p.get("account") != account:
                continue
            if e.get("type") == "signal":
                opened[p["id"]] = e["ts_ms"]
            elif e.get("type") == "close":
                t0 = opened.get(p["id"])
                if t0 is not None and t0 >= split_ms and e["ts_ms"] <= end_ms:
                    out.append({"id": p["id"], "opened_ms": t0, "closed_ms": e["ts_ms"],
                                "net_ret": p["net_ret"], "gross_ret": p["gross_ret"]})
    return out


def _split(summary: dict, cli: str | None) -> tuple[str, str]:
    if cli:
        return cli, "cli"
    s = summary.get("window", {}).get("is_oos_split")
    if s:
        return s, "summary"
    return DEFAULT_IS_OOS_SPLIT, "default (pre-registered in docs/loop/designs/T3.0-T3.1.md)"


def trial_sharpe(run: Path) -> float | None:
    """OOS per-trade Sharpe of one logged trial, recomputed the same way as the gated run."""
    try:
        s = json.loads((run / "summary.json").read_text(encoding="utf-8"))
        split, _ = _split(s, None)
        tr = oos_trades(run, _ms(split), _ms(s["window"]["to"]))
    except (OSError, KeyError, ValueError):
        return None
    return sharpe_moments([t["net_ret"] for t in tr])["sr"]


# Fields of a trial-log line that define the hypothesis. Everything else (run_id, broker, costs, fill model,
# holdout_opened, mode) describes how or when it was executed, not what was tried.
TRIAL_IDENTITY = ("strategy", "params", "symbols", "trade_from", "to")


def trial_key(t: dict) -> str:
    """Canonical identity of one logged run's hypothesis (see the module docstring)."""
    syms = t.get("symbols")
    ident = {"strategy": t.get("strategy"), "params": t.get("params") or {},
             "symbols": sorted(syms) if syms else None, "trade_from": t.get("trade_from"), "to": t.get("to")}
    return json.dumps(ident, sort_keys=True, separators=(",", ":"))


def distinct_trials(trials: list[dict]) -> list[dict]:
    """Group logged runs by trial_key, in first-logged order: [{key, run_ids}] (run_ids in log order)."""
    groups: dict[str, dict] = {}
    for t in trials:
        k = trial_key(t)
        groups.setdefault(k, {"key": json.loads(k), "run_ids": []})["run_ids"].append(t.get("run_id"))
    return list(groups.values())


def _group_sharpe(root: Path, run_ids: list, gated_run_id: str | None) -> tuple[float | None, str | None]:
    """One Sharpe per distinct trial: the gated run's if it is in this group, else the latest recoverable."""
    order = ([gated_run_id] if gated_run_id in run_ids else []) + [r for r in reversed(run_ids) if r]
    for rid in order:
        sr = trial_sharpe(root / rid)
        if sr is not None:
            return sr, rid
    return None, None


def evaluate(run: Path, trials_path: Path | None = None, split_cli: str | None = None) -> dict:
    s = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    trials_path = trials_path or run.parent / "trials.jsonl"
    split, split_src = _split(s, split_cli)
    end = s["window"]["to"]
    trades = oos_trades(run, _ms(split), _ms(end))

    trials = [json.loads(x) for x in trials_path.read_text(encoding="utf-8").splitlines() if x.strip()] \
        if trials_path.exists() else []
    groups = distinct_trials(trials)
    n_trials = max(len(groups), 1)
    sharpes = None
    if n_trials > 1:
        sharpes = []
        for g in groups:
            sr, src = _group_sharpe(trials_path.parent, g["run_ids"], s.get("run_id"))
            g["sharpe"], g["sharpe_from"] = sr, src
            sharpes.append(sr)

    root = trials_path.parent
    reps = []
    for g in groups or [{"run_ids": [s.get("run_id")]}]:
        ids = [r for r in g["run_ids"] if r]
        pick = s.get("run_id") if s.get("run_id") in ids else next((r for r in ids if (root / r / "equity_daily.json").exists()), ids[0] if ids else None)
        reps.append(run if pick == s.get("run_id") else root / pick)
    # L2a: runs saved with a daily curve use the daily DSR; legacy runs (made before L2a) keep the per-trade
    # basis, labelled as such, until they are re-run
    dsr_daily = daily_dsr(run, reps, _ms(split), _ms(end)) if (run / "equity_daily.json").exists() else None

    acc, bh = s["accounts"]["net"], s.get("buy_and_hold", {}).get("net", {})
    exits = s.get("params", {}).get("exits")
    res = verdict(oos_net_trade_returns=[t["net_ret"] for t in trades],
                  oos_gross_trade_returns=[t["gross_ret"] for t in trades],
                  full_net_return=acc.get("return_liquidated"),
                  oos_net_return=acc.get("periods", {}).get("OOS", {}).get("ret"),
                  oos_btc_net_return=bh.get("periods", {}).get("OOS", {}).get("ret"),
                  regime=s.get("regime", {}).get("OOS"), cost_model=s["costs"]["net"], symbols=s.get("symbols"),
                  n_trials=n_trials, trial_sharpes=sharpes, exits=exits, dsr_daily=dsr_daily,
                  benchmarks=benchmark_check(run, _ms(split), _ms(end)),
                  target_weeks=target_weeks(run, _ms(split), _ms(end)))
    res["checks"]["dsr_basis"] = ("OOS daily net returns (L2a)" if dsr_daily is not None
                                  else "OOS per-trade net returns (legacy run without a daily curve; re-run for L2a)")
    res["checks"]["dsr"].update(
        n_logged_runs=len(trials), trials=groups,
        trial_rule="N = distinct (strategy, params, symbols, trade window) in the trial log; re-runs of the "
                   "same set on another cost model or broker are the same trial (raw log kept intact)")
    return {"mode": "REPLAY", "label": LABEL, "run_id": s.get("run_id"), "strategy": s.get("strategy"),
            "params": s.get("params"), "verdict": res["verdict"], "reasons": res["reasons"],
            "oos_window": {"from": split, "to": end, "split_source": split_src,
                           "trade_rule": "opened at or after the split, closed by the window end"},
            "trials_log": str(trials_path), "checks": res["checks"],
            "gated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "caveats": ["Observations for DSR are OOS per-trade net returns (the equity curve is not persisted).",
                        "N counts distinct logged parameter sets only; choices made before logging (e.g. SMA50 chosen over "
                        "100/150/200 by the live script's author) are not in N, so N is a lower bound.",
                        "Fees, spread and slippage are the run's stated assumptions (real values UNKNOWN).",
                        *s.get("caveats", [])]}


def _p(x) -> str:
    return "UNKNOWN" if x is None else f"{x:+.2%}"


def _wr(x) -> str:
    return "UNKNOWN" if x is None else f"{x:.1%}"


def markdown(r: dict) -> str:
    c = r["checks"]
    e, d, b = c["trade_economics"], c["dsr"], c["btc_benchmark"]
    lines = [f"# Gate: {r['strategy']} `{r['run_id']}`", "", f"**{LABEL}**", "",
             f"**Verdict: {r['verdict']}**", "", *[f"- {x}" for x in r["reasons"]], "",
             "| check | value | result |", "|---|---|---|",
             f"| OOS trades ({r['oos_window']['from']} to {r['oos_window']['to']}) | {c['oos_trades']['value']} "
             f"| {'ok' if c['oos_trades']['ok'] else f'< {MIN_OOS_TRADES}'} |",
             f"| shared-account net (full window, after costs) | {_p(c['shared_account_net']['value'])} "
             f"| {'ok' if c['shared_account_net']['ok'] else 'not positive'} |",
             f"| OOS net | {_p(c['oos_net']['value'])} | {'ok' if c['oos_net']['ok'] else 'not positive'} |",
             f"| BTC buy&hold OOS net (flag only) | {_p(b['btc_buy_hold_oos_net'])} "
             f"| {'beats BTC' if b['beats_btc'] else 'does NOT beat BTC' if b['beats_btc'] is False else 'UNKNOWN'} |",
             f"| OOS regime | {c['regime']['label']} | {'stated' if c['regime']['ok'] else 'UNKNOWN'} |"]
    if e["mode"] == "fixed_exits":
        lines.append(f"| win rate vs break-even p* | {_wr(e['win_rate_net'])} vs {_wr(e['breakeven_win_rate'])} "
                     f"| {'ok' if e['ok'] else 'below p*'} |")
    else:
        lines.append(f"| mean net per trade (gross vs round-trip cost) | {_p(e['mean_net_return_per_trade'])} "
                     f"({_p(e['mean_gross_return_per_trade'])} vs {e['round_trip_cost']:.3%}); win rate "
                     f"{_wr(e['win_rate_net'])} | {'ok' if e['ok'] else 'not positive'} |")
    dv = "not computed: " + d.get("why_not_computed", "") if d["value"] is None else f"{d['value']:.4f}"
    runs = d.get("n_logged_runs")
    lines.append(f"| DSR (N = {d['n_trials']} distinct parameter sets"
                 f"{'' if runs is None else f' from {runs} logged runs'}, T = {d['t']}, SR/trade = "
                 f"{'UNKNOWN' if d['sr_per_trade'] is None else round(d['sr_per_trade'], 4)}, "
                 f"skew {'UNKNOWN' if d['skew'] is None else round(d['skew'], 2)}, kurt "
                 f"{'UNKNOWN' if d['kurtosis'] is None else round(d['kurtosis'], 2)}) | {dv} "
                 f"| {'ok' if d['ok'] else f'< {DSR_THRESHOLD} or not computed'} |")
    if d.get("sr0_critical") is not None:
        lines += ["", f"Robustness: the DSR stays >= {DSR_THRESHOLD} as long as the expected maximum per-trade SR "
                      f"of all trials (logged or not) is below {d['sr0_critical']:.4f} (this run's SR/trade is "
                      f"{d['sr_per_trade']:.4f})."]
    lines += ["", "Caveats:", *[f"- {x}" for x in r["caveats"]], ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="run directory containing summary.json and events.jsonl")
    ap.add_argument("--trials", default=None, help="trial log; default <run>/../trials.jsonl")
    ap.add_argument("--is-oos-split", default=None, help="YYYY-MM; default: the run's summary, else pre-registered")
    a = ap.parse_args(argv)
    run = Path(a.run)
    rep = evaluate(run, Path(a.trials) if a.trials else None, a.is_oos_split)
    (run / "gate.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    md = markdown(rep)
    (run / "gate.md").write_text(md, encoding="utf-8")
    print(md)
    return rep


if __name__ == "__main__":
    main()
