"""L2a: equity_daily.json and the gate's DSR on OOS daily returns (pre-registered in docs/loop/designs/L2a-daily-dsr.md)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from trading.replay import gate, metrics

DAY = 86_400_000


def test_daily_series_takes_the_last_mark_of_each_utc_day():
    ts = np.array([DAY * 10 + 60_000, DAY * 10 + 120_000, DAY * 11 + 60_000, DAY * 12 + 5], dtype=np.int64)
    eq = np.array([100.0, 101.0, 99.0, 103.0])
    assert metrics.daily_series(ts, eq) == [[DAY * 10, 101.0], [DAY * 11, 99.0], [DAY * 12, 103.0]]


def test_oos_daily_returns_slice_starts_from_the_last_day_before_the_split():
    series = [[DAY * d, v] for d, v in [(1, 100.0), (2, 110.0), (3, 99.0), (4, 108.9)]]
    r = gate.oos_daily_returns(series, t0=DAY * 2, t1=DAY * 5)
    assert r == pytest.approx([99.0 / 110.0 - 1, 108.9 / 99.0 - 1])


def _run(tmp: Path, rid: str, daily: list | None) -> Path:
    d = tmp / rid
    d.mkdir(parents=True)
    if daily is not None:
        (d / "equity_daily.json").write_text(json.dumps({"net": daily}), encoding="utf-8")
    return d


def _walk(seed: int, n: int = 400, drift: float = 0.001) -> list:
    rng = np.random.default_rng(seed)
    eq = 1000 * np.cumprod(1 + drift + 0.02 * rng.standard_normal(n))
    return [[DAY * (100 + i), float(v)] for i, v in enumerate(eq)]


def test_daily_dsr_uses_every_trials_daily_sharpe(tmp_path):
    runs = [_run(tmp_path, f"r{i}", _walk(i)) for i in range(4)]
    out = gate.daily_dsr(runs[0], runs, t0=DAY * 150, t1=DAY * 600)
    assert out["basis"] == "OOS daily net returns" and out["n_trials"] == 4
    assert out["value"] is not None and 0.0 <= out["value"] <= 1.0
    assert out["t"] == 349          # days 100..499 in (150, 600): returns from day 150 to day 499


def test_daily_dsr_is_not_computed_when_any_trial_lacks_a_daily_curve(tmp_path):
    runs = [_run(tmp_path, "a", _walk(1)), _run(tmp_path, "b", None)]
    out = gate.daily_dsr(runs[0], runs, t0=DAY * 150, t1=DAY * 600)
    assert out["value"] is None
    assert "lacks a daily curve" in out["why_not_computed"] and "b" in out["why_not_computed"]


def test_daily_dsr_single_trial_is_psr_against_zero(tmp_path):
    run = _run(tmp_path, "solo", _walk(7, drift=0.004))
    out = gate.daily_dsr(run, [run], t0=DAY * 150, t1=DAY * 600)
    assert out["sr0"] == 0.0 and out["value"] > 0.95
