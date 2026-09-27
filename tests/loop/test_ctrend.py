"""A2 CTREND-lite: indicators, ranks, elastic-net selection, and a full replay without look-ahead."""
from __future__ import annotations

import numpy as np
import pytest
from test_replay import daily_minutes, ms

from trading.replay.broker import CostModel, PaperAccount
from trading.replay.engine import replay
from trading.replay.plugins import ctrend as C
from trading.scanner.registry import DEFAULT


def _series(n=260, drift=0.001, seed=0):
    rng = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + drift + 0.02 * rng.standard_normal(n))
    return c * 1.01, c * 0.99, c, np.full(n, 1e6) * (1 + 0.1 * rng.random(n))


def test_the_28_indicators_are_finite_and_match_hand_computed_values():
    h, l, c, qv = _series()
    v = C.indicators(h, l, c, qv)
    assert v is not None and v.shape == (28,) and np.all(np.isfinite(v))
    assert v[5] == pytest.approx(c[-3:].mean() / c[-1])          # sma_3 / close
    assert v[11] == pytest.approx(c[-200:].mean() / c[-1])        # sma_200 / close
    assert v[14] == pytest.approx(qv[-3:].mean() / qv[-1])        # volsma_3 / volume
    assert 0 <= v[0] <= 100                                        # rsi


def test_too_little_history_gives_no_indicators():
    h, l, c, qv = _series(n=150)
    assert C.indicators(h, l, c, qv) is None


def test_ranks_map_to_minus_half_plus_half_with_ties_averaged():
    r = C.rank_half(np.array([[3.0], [1.0], [2.0], [2.0]]))
    assert r[:, 0].tolist() == pytest.approx([0.5, -0.5, 0.0, 0.0])


def test_the_elastic_net_keeps_the_informative_forecast():
    rng = np.random.default_rng(1)
    F = rng.standard_normal((600, 5)) * 0.01
    y = 1.0 * F[:, 2] + 0.002 * rng.standard_normal(600)
    assert 2 in C.select_forecasts(F, y).tolist()


def test_registered_with_the_gate_family():
    from trading.replay.gate import family_of
    info = DEFAULT.get("ctrend_lite")
    assert info.family == family_of("ctrend_lite") == "cross_section"


def test_full_replay_trades_the_top_quintile_and_never_sees_the_future():
    d0 = ms("2019-01-07T00:00:00")                                  # a Monday
    n_days = 1150
    rng = np.random.default_rng(7)
    syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "BNBUSDT",
            "AVAXUSDT"]
    streams = {}
    for i, s in enumerate(syms):
        drift = 0.0004 * (i - 4)                                    # persistent trends: some coins keep rising
        c = 100 * np.cumprod(1 + drift + 0.02 * rng.standard_normal(n_days))
        streams[s] = daily_minutes(s, d0, c.tolist(), per_day=2)
    strat = DEFAULT.build("ctrend_lite")
    acct = PaperAccount(1000, 2, CostModel(0, 0, 0))                # 10 coins // 5 = top 2
    res = replay(streams, [strat], {"net": acct})                  # raises LookAheadError on any peek
    assert res.signals > 0 and strat.last_ctrend
    assert len(acct.positions) <= 2
    top = sorted(strat.last_ctrend, key=lambda s: -strat.last_ctrend[s])[:2]
    assert set(acct.positions) == set(top)                          # holds exactly the latest top quintile
    opened = [e["payload"]["symbol"] for e in res.signal_events if e["payload"]["action"] == "open"]
    strong, weak = set(syms[-3:]), set(syms[:3])                    # highest / lowest drift
    assert sum(s in strong for s in opened) > sum(s in weak for s in opened)   # it prefers the persistent trends
