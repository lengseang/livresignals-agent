"""P0b: equal-weight basket benchmark + the gate rule "beat BTC hold AND the basket" (docs/loop/designs/P0b-benchmarks.md)."""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
from test_replay import daily_minutes, ms

from trading.replay import gate
from trading.replay.broker import CostModel, PaperAccount
from trading.replay.engine import replay

DAY = 86_400_000


def test_the_basket_buys_every_coin_once_with_an_equal_share():
    d0 = ms("2021-01-01T00:00:00")
    streams = {"BTCUSDT": daily_minutes("BTCUSDT", d0, [100.0] * 4),
               "ETHUSDT": daily_minutes("ETHUSDT", d0, [100.0, 100.0, 200.0, 200.0])}
    basket = PaperAccount(1000, 2, CostModel(0, 0, 0), name="bh_ew_net")
    replay(streams, [], {"net": PaperAccount(1000, 1, CostModel(0, 0, 0))},
           benchmarks={"ew_net": (("BTCUSDT", "ETHUSDT"), basket)})
    assert len(basket.positions) == 2
    assert basket.equity() == pytest.approx(1500.0)            # 500 flat BTC + 500 ETH that doubled


def _run(tmp: Path, **curves) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "equity_daily.json").write_text(json.dumps(curves), encoding="utf-8")
    return tmp


def _walk(seed: int, drift: float, n: int = 400) -> list:
    rng = np.random.default_rng(seed)
    eq = 1000 * np.cumprod(1 + drift + 0.02 * rng.standard_normal(n))
    return [[DAY * (100 + i), float(v)] for i, v in enumerate(eq)]


def test_a_strategy_must_beat_both_benchmarks(tmp_path):
    run = _run(tmp_path / "a", net=_walk(1, 0.004), bh_net=_walk(2, 0.0005), bh_ew_net=_walk(3, 0.0005))
    out = gate.benchmark_check(run, DAY * 150, DAY * 600)
    assert out["ok"] is True and out["strategy_sr_ann"] > max(out["benchmarks_sr_ann"].values())


def test_losing_to_one_benchmark_fails(tmp_path):
    run = _run(tmp_path / "b", net=_walk(1, 0.001), bh_net=_walk(2, 0.0), bh_ew_net=_walk(3, 0.006))
    out = gate.benchmark_check(run, DAY * 150, DAY * 600)
    assert out["ok"] is False


def test_a_run_without_the_basket_curve_is_not_computable_not_guessed(tmp_path):
    run = _run(tmp_path / "c", net=_walk(1, 0.004), bh_net=_walk(2, 0.0))
    out = gate.benchmark_check(run, DAY * 150, DAY * 600)
    assert out["ok"] is None and "bh_ew_net" in out["why_not_computed"]


def test_the_verdict_fails_and_names_the_benchmark_it_lost_to():
    res = gate.verdict(oos_net_trade_returns=[0.01] * 40, oos_gross_trade_returns=[0.012] * 40,
                       full_net_return=0.5, oos_net_return=0.2, oos_btc_net_return=0.1, regime=None,
                       cost_model={"fee_rate": 0, "spread_bps": 0, "slippage_bps": 0}, n_trials=1,
                       benchmarks={"ok": False, "strategy_sr_ann": 0.5,
                                   "benchmarks_sr_ann": {"btc_hold": 0.4, "equal_weight_hold": 0.9}})
    assert res["verdict"] == gate.FAIL
    assert any("equal_weight_hold 0.90/yr" in r and "btc_hold" not in r for r in res["reasons"])
