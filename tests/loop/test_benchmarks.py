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


def test_a_coin_listed_late_still_gets_its_one_nth_share():
    # the real failure: early coins rise, so min(cash, equity/N) over-spends on the next listing and the last
    # coin finds no cash (full history: DOT, AVAX, NEAR, ARB, SUI were never bought)
    d0 = ms("2021-01-01T00:00:00")
    up = [100.0, 200.0, 200.0, 200.0, 200.0]
    streams = {"BTCUSDT": daily_minutes("BTCUSDT", d0, up), "ETHUSDT": daily_minutes("ETHUSDT", d0, up),
               "SOLUSDT": daily_minutes("SOLUSDT", d0 + 2 * DAY, [50.0, 50.0, 50.0]),
               "DOTUSDT": daily_minutes("DOTUSDT", d0 + 3 * DAY, [20.0, 20.0])}
    basket = PaperAccount(1200, 4, CostModel(0, 0, 0), name="bh_ew_net")
    basket.fixed_budget = 300.0
    replay(streams, [], {"net": PaperAccount(1200, 1, CostModel(0, 0, 0))},
           benchmarks={"ew_net": (("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOTUSDT"), basket)})
    assert {s: round(p["spent"], 6) for s, p in basket.positions.items()} == {
        "BTCUSDT": 300.0, "ETHUSDT": 300.0, "SOLUSDT": 300.0, "DOTUSDT": 300.0}


def _verdict(**kw):
    base = dict(oos_net_trade_returns=[], oos_gross_trade_returns=[], full_net_return=0.5, oos_net_return=0.2,
                oos_btc_net_return=0.1, regime={"label": "bull"}, cost_model={"fee_rate": 0, "spread_bps": 0,
                "slippage_bps": 0}, n_trials=1)
    return gate.verdict(**{**base, **kw})


def test_p0d_a_target_weight_run_is_judged_on_active_weeks_not_closed_trades():
    res = _verdict(target_weeks=217, dsr_daily={"value": 0.5, "n_trials": 31, "t": 1519})
    assert res["checks"]["oos_trades"]["ok"] and res["checks"]["trade_economics"]["mode"] == "not_applicable"
    assert not any("OOS trades" in r or "per-trade" in r for r in res["reasons"])


def test_p0d_too_few_active_weeks_is_insufficient():
    res = _verdict(target_weeks=12)
    assert res["verdict"] == gate.INSUFFICIENT and any("12 OOS weeks" in r for r in res["reasons"])


def test_p0d_open_close_runs_keep_the_closed_trade_rule():
    res = _verdict()
    assert any("only 0 OOS trades" in r for r in res["reasons"])


def test_p0d_target_weeks_detects_target_runs_from_their_events(tmp_path):
    run = tmp_path / "r"
    run.mkdir()
    W = 7 * DAY
    ev = [{"type": "strategy_signal", "ts_ms": 10 * W, "payload": {"action": "target"}},
          {"type": "rebalance", "ts_ms": 10 * W + 5, "payload": {"account": "net"}},
          {"type": "rebalance", "ts_ms": 10 * W + 9, "payload": {"account": "net"}},       # same week
          {"type": "rebalance", "ts_ms": 12 * W, "payload": {"account": "gross"}},         # other account
          {"type": "close", "ts_ms": 13 * W, "payload": {"account": "net"}}]
    (run / "events.jsonl").write_text("\n".join(json.dumps(e) for e in ev), encoding="utf-8")
    assert gate.target_weeks(run, 0, 100 * W) == 2
    (run / "events.jsonl").write_text(json.dumps({"type": "close", "ts_ms": 1, "payload": {"account": "net"}}),
                                      encoding="utf-8")
    assert gate.target_weeks(run, 0, 100 * W) is None                                    # an open/close run


def test_p0d_a_target_run_without_a_dsr_never_passes():
    res = _verdict(target_weeks=217)                                  # no daily DSR, no closed trades
    assert res["verdict"] == gate.INSUFFICIENT and any("needs the daily DSR" in r for r in res["reasons"])


def test_p0d_a_target_run_that_clears_everything_passes_with_a_readable_reason():
    res = _verdict(target_weeks=217, dsr_daily={"value": 0.97, "n_trials": 31, "t": 1519},
                   benchmarks={"ok": True, "strategy_sr_ann": 0.9, "benchmarks_sr_ann": {"btc_hold": 0.4}})
    assert res["verdict"] == gate.PASS and "217 OOS weeks" in res["reasons"][0]
