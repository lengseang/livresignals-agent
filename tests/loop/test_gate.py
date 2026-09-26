"""Tests for the T3.3 validation gate (trading.replay.gate).

The Deflated Sharpe numbers are checked against the published worked example in
M. López de Prado, "Deflating the Sharpe Ratio" (slides, SSRN 2465675, "Numerical Example (1/2)" and
"(2/2)"; text copy at https://pdfs.semanticscholar.org/c215/d0a2064ce1a3565d276475abc84305418f0f.pdf),
which restates the example of Bailey & López de Prado, "The Deflated Sharpe Ratio", JPM 2014
(SSRN 2460551, section "A Numerical Example"):

    daily strategy, annualised SR = 2.5, N = 100 independent trials, V[SR_n] = 1/2 (annualised),
    T = 1250, skewness = -3, kurtosis = 10, 250 observations per year
    -> SR0 = 0.1132 (non-annualised), DSR = 0.9004
    -> with N = 46 the DSR would be 0.9505
    -> with Normal returns (skew 0, kurtosis 3) DSR = 0.9505 after N = 88

Everything else is tested by properties, not invented expected values.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from trading.replay import gate

# ----- the published worked example -------------------------------------------------------------
SR_ANN, OBS_PER_YEAR = 2.5, 250
SR = SR_ANN / math.sqrt(OBS_PER_YEAR)          # non-annualised
V_SR = 0.5 / OBS_PER_YEAR                      # V[SR_n] = 1/2 annualised -> per-observation units


def test_expected_max_sharpe_matches_published_example():
    assert round(gate.expected_max_sharpe(V_SR, 100), 4) == 0.1132


def test_dsr_matches_published_example_n100():
    sr0 = gate.expected_max_sharpe(V_SR, 100)
    assert round(gate.psr(SR, 1250, -3, 10, sr0), 4) == 0.9004
    assert round(gate.deflated_sharpe(SR, 1250, -3, 10, n_trials=100, var_trial_sr=V_SR), 4) == 0.9004


def test_dsr_matches_published_example_n46_and_normal_n88():
    assert round(gate.deflated_sharpe(SR, 1250, -3, 10, n_trials=46, var_trial_sr=V_SR), 4) == 0.9505
    assert round(gate.deflated_sharpe(SR, 1250, 0, 3, n_trials=88, var_trial_sr=V_SR), 4) == 0.9505


# ----- properties --------------------------------------------------------------------------------
def test_dsr_equals_psr_against_zero_when_one_trial():
    assert gate.expected_max_sharpe(V_SR, 1) == 0.0
    assert gate.deflated_sharpe(SR, 1250, -3, 10, n_trials=1, var_trial_sr=None) == pytest.approx(
        gate.psr(SR, 1250, -3, 10, 0.0))


def test_dsr_decreases_as_trials_grow():
    vals = [gate.deflated_sharpe(SR, 1250, -3, 10, n_trials=n, var_trial_sr=V_SR) for n in (2, 5, 10, 50, 100, 1000)]
    assert all(a > b for a, b in zip(vals, vals[1:]))


def test_dsr_increases_with_observed_sharpe():
    vals = [gate.deflated_sharpe(s, 1250, 0, 3, n_trials=20, var_trial_sr=V_SR) for s in (0.0, 0.05, 0.1, 0.2)]
    assert all(a < b for a, b in zip(vals, vals[1:]))


def test_dsr_needs_trial_variance_when_more_than_one_trial():
    with pytest.raises(ValueError):
        gate.deflated_sharpe(SR, 1250, 0, 3, n_trials=5, var_trial_sr=None)


def test_psr_undefined_when_variance_term_not_positive():
    # 1 - skew*SR + (kurt-1)/4*SR^2 <= 0 has no meaning as a variance: refuse rather than guess
    with pytest.raises(ValueError):
        gate.psr(1.0, 100, 5.0, 3.0, 0.0)


def test_critical_sr0_is_where_psr_hits_the_threshold():
    s0 = gate.critical_sr0(SR, 1250, -3, 10)
    assert gate.psr(SR, 1250, -3, 10, s0) == pytest.approx(gate.DSR_THRESHOLD)
    # consistent with the published example: SR0 = 0.1132 at N = 100 fails, 0.1004 at N = 46 passes
    assert 0.1004 < s0 < 0.1132


def test_sharpe_moments_of_a_simple_series():
    x = [0.1, -0.05, 0.2, 0.0, -0.1, 0.3]
    m = gate.sharpe_moments(x)
    mean = sum(x) / len(x)
    sd = math.sqrt(sum((v - mean) ** 2 for v in x) / (len(x) - 1))
    assert m["n"] == 6 and m["sr"] == pytest.approx(mean / sd)
    assert m["kurtosis"] > 0 and "skew" in m      # raw (non-excess) kurtosis


def test_breakeven_win_rate_formula():
    # p* = (sl + c) / (tp + sl), fees-and-breakeven.md; TP 1%, SL 1%, c = 0.2% -> 0.6
    assert gate.breakeven_win_rate(0.01, 0.01, 0.002) == pytest.approx(0.6)


def test_round_trip_cost_from_cost_model():
    c = gate.round_trip_cost({"fee_rate": 0.001, "spread_bps": 2.0, "slippage_bps": 2.0})
    assert c == pytest.approx(2 * (0.001 + 0.0001 + 0.0002))


# ----- verdict -----------------------------------------------------------------------------------
def _inputs(n_oos=40, mean=0.05, oos_ret=0.3, full_ret=0.5, bh_oos=0.1, dsr_n=1, var=None, exits=None,
            regime="bull, high-vol"):
    rets = [mean + (0.1 if i % 2 else -0.1) for i in range(n_oos)]
    return dict(
        oos_net_trade_returns=rets, oos_gross_trade_returns=[r + 0.003 for r in rets],
        full_net_return=full_ret, oos_net_return=oos_ret, oos_btc_net_return=bh_oos,
        regime={"label": regime}, cost_model={"fee_rate": 0.001, "spread_bps": 2.0, "slippage_bps": 2.0},
        n_trials=dsr_n, trial_sharpes=var, exits=exits)


def test_verdict_never_pass_below_min_oos_trades():
    v = gate.verdict(**_inputs(n_oos=gate.MIN_OOS_TRADES - 1, mean=0.5))
    assert v["verdict"] == gate.INSUFFICIENT
    assert any("OOS trades" in r for r in v["reasons"])


def test_verdict_pass_when_every_check_holds():
    v = gate.verdict(**_inputs(n_oos=60, mean=0.08))
    assert v["verdict"] == gate.PASS, v["reasons"]
    assert v["checks"]["dsr"]["value"] >= gate.DSR_THRESHOLD


def test_verdict_fails_on_negative_oos_net():
    v = gate.verdict(**_inputs(oos_ret=-0.01))
    assert v["verdict"] == gate.FAIL and any("OOS net" in r for r in v["reasons"])


def test_verdict_fails_on_negative_shared_account_net():
    v = gate.verdict(**_inputs(full_ret=-0.2))
    assert v["verdict"] == gate.FAIL


def test_verdict_fails_on_low_dsr():
    v = gate.verdict(**_inputs(n_oos=40, mean=0.001))
    assert v["verdict"] == gate.FAIL and any("DSR" in r for r in v["reasons"])


def test_verdict_fails_when_mean_net_per_trade_not_positive():
    v = gate.verdict(**_inputs(mean=-0.01))
    assert v["verdict"] == gate.FAIL and any("per trade" in r for r in v["reasons"])


def test_btc_benchmark_is_a_flag_not_a_failure():
    v = gate.verdict(**_inputs(n_oos=60, mean=0.08, oos_ret=0.3, bh_oos=0.9))
    assert v["verdict"] == gate.PASS
    assert v["checks"]["btc_benchmark"]["beats_btc"] is False


def test_fixed_exits_report_win_rate_beside_breakeven():
    v = gate.verdict(**_inputs(n_oos=60, mean=0.08, exits={"tp": 0.02, "sl": 0.01}))
    e = v["checks"]["trade_economics"]
    assert e["mode"] == "fixed_exits" and "breakeven_win_rate" in e and "win_rate_net" in e


def test_unknown_regime_is_insufficient():
    v = gate.verdict(**_inputs(n_oos=60, mean=0.08, regime="UNKNOWN"))
    assert v["verdict"] == gate.INSUFFICIENT


def test_multiple_trials_without_trial_sharpes_is_insufficient():
    v = gate.verdict(**_inputs(n_oos=60, mean=0.08, dsr_n=3, var=[0.1]))
    assert v["verdict"] == gate.INSUFFICIENT and any("trial" in r for r in v["reasons"])


def test_multiple_trials_use_variance_of_trial_sharpes():
    v = gate.verdict(**_inputs(n_oos=60, mean=0.08, dsr_n=3, var=[0.1, 0.2, 0.3]))
    d = v["checks"]["dsr"]
    assert d["n_trials"] == 3 and d["var_trial_sr"] == pytest.approx(0.01)


# ----- CLI on a small run directory --------------------------------------------------------------
def _write_run(root: Path, n_trades: int, ret: float) -> Path:
    run = root / "r1"
    run.mkdir(parents=True)
    split = 1640995200000                                          # 2022-01-01
    summary = {
        "mode": "REPLAY", "run_id": "r1", "strategy": "s", "params": {},
        "costs": {"net": {"fee_rate": 0.001, "spread_bps": 2.0, "slippage_bps": 2.0}},
        "window": {"trade_from": "2021-01-01T00:00:00Z", "to": "2023-01-01T00:00:00Z", "is_oos_split": "2022-01"},
        "accounts": {"net": {"return_liquidated": 0.4, "periods": {"OOS": {"ret": 0.2}, "full": {"ret": 0.4}}}},
        "buy_and_hold": {"net": {"periods": {"OOS": {"ret": 0.1}}}},
        "regime": {"OOS": {"label": "range, low-vol"}},
    }
    (run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    with open(run / "events.jsonl", "w", encoding="utf-8") as f:
        for i in range(n_trades):
            t = split + i * 86_400_000
            sid = f"s:X:{t}"
            f.write(json.dumps({"type": "signal", "ts_ms": t, "payload": {"id": sid, "account": "net"}}) + "\n")
            r = ret + (0.01 if i % 2 else -0.01)
            f.write(json.dumps({"type": "close", "ts_ms": t + 3_600_000,
                                "payload": {"id": sid, "account": "net", "net_ret": r, "gross_ret": r + 0.003}}) + "\n")
    (root / "trials.jsonl").write_text(json.dumps({"run_id": "r1", "strategy": "s"}) + "\n", encoding="utf-8")
    return run


def test_cli_writes_report_json_and_markdown(tmp_path):
    run = _write_run(tmp_path, 10, 0.02)
    rep = gate.main(["--run", str(run)])
    assert rep["verdict"] == gate.INSUFFICIENT                   # 10 OOS trades < MIN_OOS_TRADES
    j = json.loads((run / "gate.json").read_text(encoding="utf-8"))
    assert j["verdict"] == gate.INSUFFICIENT and j["mode"] == "REPLAY"
    md = (run / "gate.md").read_text(encoding="utf-8")
    assert "INSUFFICIENT_DATA" in md and "REPLAY" in md


def test_cli_counts_trials_from_log(tmp_path):
    run = _write_run(tmp_path, 40, 0.02)
    rep = gate.main(["--run", str(run)])
    assert rep["checks"]["dsr"]["n_trials"] == 1
    assert rep["checks"]["oos_trades"]["value"] == 40


def test_gate_never_imports_an_exchange_client():
    code = ("import sys, trading.replay.gate;"
            "bad=[m for m in sys.modules if m=='ccxt' or m.startswith('ccxt.') or m.startswith('trading.exchange')];"
            "print(','.join(bad))")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
    assert out.stdout.strip() == ""
