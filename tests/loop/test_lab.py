"""trading.lab: queued, pre-registered experiments run without an AI in the loop (L1a)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from trading.lab import spec as lab_spec
from trading.lab import runner

SPEC = {
    "name": "trend_grid",
    "strategy": "trend_real",
    "grid": {"sma": [50, 100], "hard_stop": [0.15, "none"]},
    "fixed": {"broker": "paper", "max_pos": 3, "symbols": "BTCUSDT,ETHUSDT"},
    "final_test": False,
    "hypothesis": "trend beats hold net of fees",
}


def _write(tmp: Path, body: dict, name: str = "trend_grid.json") -> Path:
    q = tmp / "queue"
    q.mkdir(parents=True, exist_ok=True)
    p = q / name
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


def test_grid_expands_to_every_combination_in_a_stable_order():
    jobs = lab_spec.expand(SPEC)
    assert [j["params"] for j in jobs] == [
        {"sma": 50, "hard_stop": 0.15}, {"sma": 50, "hard_stop": "none"},
        {"sma": 100, "hard_stop": 0.15}, {"sma": 100, "hard_stop": "none"},
    ]
    assert len({j["job_id"] for j in jobs}) == 4


def test_job_maps_to_replay_cli_flags():
    job = lab_spec.expand(SPEC)[0]
    args = lab_spec.cli_args(SPEC, job)
    assert args[:2] == ["--strategy", "trend_real"]
    for flag, val in [("--sma", "50"), ("--hard-stop", "0.15"), ("--broker", "paper"), ("--max-pos", "3")]:
        assert args[args.index(flag) + 1] == val
    assert "--open-holdout" not in args


def test_spec_requires_a_hypothesis_and_known_keys():
    with pytest.raises(ValueError, match="hypothesis"):
        lab_spec.validate({k: v for k, v in SPEC.items() if k != "hypothesis"})
    with pytest.raises(ValueError, match="unknown"):
        lab_spec.validate({**SPEC, "surprise": 1})


def test_spec_is_frozen_when_queued_and_edits_are_refused(tmp_path):
    p = _write(tmp_path, SPEC)
    lab_spec.freeze(p, tmp_path / "frozen")
    p.write_text(json.dumps({**SPEC, "grid": {"sma": [20]}}), encoding="utf-8")   # edited after results could exist
    with pytest.raises(ValueError, match="changed after it was frozen"):
        lab_spec.freeze(p, tmp_path / "frozen")


def test_holdout_needs_final_test_and_is_single_use(tmp_path):
    ledger = tmp_path / "holdout-ledger.jsonl"
    with pytest.raises(ValueError, match="final_test"):
        lab_spec.holdout_permission({**SPEC, "open_holdout": True}, ledger)
    final = {**SPEC, "grid": {"sma": [200], "hard_stop": [0.15]}, "open_holdout": True, "final_test": True}
    lab_spec.holdout_permission(final, ledger)             # first use: allowed and recorded
    assert ledger.exists()
    with pytest.raises(ValueError, match="already used"):
        lab_spec.holdout_permission(final, ledger)
    args = lab_spec.cli_args(final, lab_spec.expand(final)[0])
    assert "--open-holdout" in args


def test_final_test_must_be_a_single_frozen_candidate():
    with pytest.raises(ValueError, match="single"):
        lab_spec.validate({**SPEC, "final_test": True, "open_holdout": True})


def test_runner_is_resumable_and_writes_a_digest(tmp_path):
    p = _write(tmp_path, SPEC)
    calls = []

    def fake_replay(args: list[str], out_root: Path) -> dict:
        calls.append(args)
        sma = args[args.index("--sma") + 1]
        return {"run_dir": str(out_root / f"run-{sma}-{len(calls)}"), "rc": 0, "seconds": 0.01}

    def fake_gate(run_dir: str) -> dict:
        return {"verdict": "FAIL", "dsr": 0.1, "n_trials": 9, "reasons": ["DSR 0.1 < 0.95"]}

    lab = runner.Lab(tmp_path, workers=2, replay=fake_replay, gate=fake_gate)
    summary = lab.run_queue()
    assert summary["jobs_run"] == 4 and summary["jobs_skipped"] == 0
    assert (tmp_path / "results.jsonl").read_text().count("\n") == 4
    digest = next((tmp_path / "digests").glob("digest-*.md")).read_text(encoding="utf-8")
    assert "trend_grid" in digest and "FAIL" in digest and "pre-registered" in digest

    again = runner.Lab(tmp_path, workers=2, replay=fake_replay, gate=fake_gate).run_queue()
    assert again["jobs_run"] == 0 and again["jobs_skipped"] == 4       # done jobs are not re-run
    assert len(calls) == 4


def test_a_crashed_job_is_recorded_and_retried_next_time(tmp_path):
    _write(tmp_path, {**SPEC, "grid": {"sma": [50], "hard_stop": [0.15]}})
    boom = {"n": 0}

    def flaky(args, out_root):
        boom["n"] += 1
        if boom["n"] == 1:
            return {"run_dir": None, "rc": 1, "seconds": 0.0, "stderr_tail": "Traceback: boom"}
        return {"run_dir": str(out_root / "ok"), "rc": 0, "seconds": 0.0}

    gate = lambda d: {"verdict": "FAIL", "dsr": 0.0, "n_trials": 1, "reasons": []}
    first = runner.Lab(tmp_path, workers=1, replay=flaky, gate=gate).run_queue()
    assert first["jobs_failed"] == 1
    second = runner.Lab(tmp_path, workers=1, replay=flaky, gate=gate).run_queue()
    assert second["jobs_run"] == 1 and second["jobs_failed"] == 0


def test_lab_never_imports_an_exchange_client():
    import subprocess
    code = ("import sys, trading.lab.runner, trading.lab.spec;"
            "bad=[m for m in sys.modules if m=='ccxt' or m.startswith('ccxt.') or m.startswith('trading.exchange')];"
            "print(','.join(bad))")
    env = {"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
    assert out.stdout.strip() == ""
