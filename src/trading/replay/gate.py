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
shared "net" account), because the replay does not persist its equity curve. Every logged trial counts
toward N, including variants that were looked at and discarded.
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

    Legacy flat cost model: fee_rate / spread_bps / slippage_bps. Paper-broker header (T4.2): fee_taker and the
    measured spread at its spread_stat; the WIDEST symbol of the run is used, so p* is the conservative one.
    """
    if "fee_rate" in cost_model:
        return 2 * (cost_model["fee_rate"] + (cost_model["spread_bps"] / 2 + cost_model["slippage_bps"]) / 1e4)
    from trading.paper import spreads
    stat, over = cost_model.get("spread_stat", "median"), cost_model.get("spread_override")
    spread = 0.0 if stat == "none" else max((spreads.spread_bp(s, stat, over) for s in symbols or ()), default=0.0)
    return 2 * (cost_model["fee_taker"] + spread / 2 / 1e4)


def breakeven_win_rate(tp: float, sl: float, cost: float) -> float:
    """p* = (sl + c) / (tp + sl): expectancy is zero at this win rate (same formula as research/07-breakeven.py)."""
    return (sl + cost) / (tp + sl)


# ----- verdict -----------------------------------------------------------------------------------
def verdict(*, oos_net_trade_returns, oos_gross_trade_returns, full_net_return, oos_net_return,
            oos_btc_net_return, regime, cost_model, n_trials, trial_sharpes=None, exits=None,
            symbols=None) -> dict:
    fails, short, checks = [], [], {}
    net = [float(r) for r in oos_net_trade_returns]
    gross = [float(r) for r in oos_gross_trade_returns]
    n = len(net)

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
    mean_net = float(np.mean(net)) if net else None
    mean_gross = float(np.mean(gross)) if gross else None
    win = (sum(r > 0 for r in net) / n) if n else None
    econ = {"round_trip_cost": c, "mean_net_return_per_trade": mean_net, "mean_gross_return_per_trade": mean_gross,
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
    if mean_net is None:
        short.append("no OOS trades, per-trade economics UNKNOWN")
    elif not exits and mean_net <= 0:
        fails.append(f"mean net return per trade {mean_net:+.3%} is not positive "
                     f"(mean gross {mean_gross:+.3%} vs round-trip cost {c:.3%})")
    checks["trade_economics"] = econ

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

    v = FAIL if fails else INSUFFICIENT if short else PASS
    reasons = fails + short if v != PASS else [
        f"{n} OOS trades >= {MIN_OOS_TRADES}; shared-account and OOS net positive; mean net per trade positive; "
        f"DSR {d['value']:.4f} >= {DSR_THRESHOLD} with N = {n_trials}"]
    return {"verdict": v, "reasons": reasons, "checks": checks}


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


def evaluate(run: Path, trials_path: Path | None = None, split_cli: str | None = None) -> dict:
    s = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    trials_path = trials_path or run.parent / "trials.jsonl"
    split, split_src = _split(s, split_cli)
    end = s["window"]["to"]
    trades = oos_trades(run, _ms(split), _ms(end))

    trials = [json.loads(x) for x in trials_path.read_text(encoding="utf-8").splitlines() if x.strip()] \
        if trials_path.exists() else []
    n_trials = max(len(trials), 1)
    sharpes = [trial_sharpe(trials_path.parent / t["run_id"]) for t in trials] if n_trials > 1 else None

    acc, bh = s["accounts"]["net"], s.get("buy_and_hold", {}).get("net", {})
    exits = s.get("params", {}).get("exits")
    res = verdict(oos_net_trade_returns=[t["net_ret"] for t in trades],
                  oos_gross_trade_returns=[t["gross_ret"] for t in trades],
                  full_net_return=acc.get("return_liquidated"),
                  oos_net_return=acc.get("periods", {}).get("OOS", {}).get("ret"),
                  oos_btc_net_return=bh.get("periods", {}).get("OOS", {}).get("ret"),
                  regime=s.get("regime", {}).get("OOS"), cost_model=s["costs"]["net"],
                  n_trials=n_trials, trial_sharpes=sharpes, exits=exits,
                  symbols=s.get("symbols"))
    return {"mode": "REPLAY", "label": LABEL, "run_id": s.get("run_id"), "strategy": s.get("strategy"),
            "params": s.get("params"), "verdict": res["verdict"], "reasons": res["reasons"],
            "oos_window": {"from": split, "to": end, "split_source": split_src,
                           "trade_rule": "opened at or after the split, closed by the window end"},
            "trials_log": str(trials_path), "checks": res["checks"],
            "gated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "caveats": ["Observations for DSR are OOS per-trade net returns (the equity curve is not persisted).",
                        "N counts logged trials only; choices made before logging (e.g. SMA50 chosen over "
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
    lines.append(f"| DSR (N = {d['n_trials']}, T = {d['t']}, SR/trade = "
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
