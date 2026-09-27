"""A1 Donchian ensemble and A3 risk-managed TSMOM (target-weight plugins, P1c)."""
from __future__ import annotations

import numpy as np
import pytest
from test_replay import daily_minutes, ms

from trading.replay.broker import CostModel, PaperAccount
from trading.replay.engine import replay
from trading.replay.plugins.donchian import LOOKBACKS, DonchianEnsemble, vol_scale
from trading.replay.plugins.tsmom_rm import TsmomRiskManaged
from trading.scanner.registry import DEFAULT


# ----- A1 ----------------------------------------------------------------------------------------------
def test_a1_every_lookback_opens_on_a_new_high_and_the_stop_only_ratchets_up():
    s = DonchianEnsemble()
    c = np.linspace(100, 200, 400)                                     # a steady rise: new highs every day
    assert s._signal("X", c) == 1.0
    stops = {n: s._stop[("X", n)] for n in LOOKBACKS}
    c2 = np.append(c, c[-1] * 1.001)
    s._signal("X", c2)
    assert all(s._stop[("X", n)] >= stops[n] for n in LOOKBACKS)


def test_a1_the_trailing_stop_never_moves_down():
    s = DonchianEnsemble(lookbacks=(5,))
    s._stop[("X", 5)] = 180.0                                            # ratcheted up on an earlier day
    c = np.array([150.0, 140, 160, 140, 160, 140, 170])                 # 5-day midpoint 150, close 170
    assert s._signal("X", c) == 0.0 and ("X", 5) not in s._stop         # 170 < 180: closed, not reset to 150


def test_a1_vol_scale_hits_the_25pct_target_and_never_levers():
    ann = lambda d: d / np.sqrt(365)
    rng = np.random.default_rng(0)
    for daily_sd, want in ((ann(0.50), 0.5), (ann(0.10), 1.0)):
        r = rng.standard_normal(5000)
        r = (r - r.mean()) / r.std(ddof=1) * daily_sd
        c = 100 * np.exp(np.cumsum(np.append(0.0, r)))
        assert vol_scale(c, 5000, 0.25) == pytest.approx(want, rel=1e-6)


def test_a1_a_fall_below_the_midpoint_closes_the_short_lookbacks_first():
    s = DonchianEnsemble()
    c = np.linspace(100, 200, 400)
    s._signal("X", c)
    drop = np.append(c, [185.0])                                       # below the 5-day midpoint, above long ones
    sig = s._signal("X", drop)
    assert 0 < sig < 1 and ("X", 5) not in s._stop and ("X", 360) in s._stop


def _streams(n_days, drifts, vol=0.02, seed=3, d0=None):
    rng = np.random.default_rng(seed)
    d0 = d0 or ms("2019-01-07T00:00:00")
    out = {}
    for sym, dr in drifts.items():
        c = 100 * np.cumprod(1 + dr + vol * rng.standard_normal(n_days))
        out[sym] = daily_minutes(sym, d0, c.tolist(), per_day=2)
    return out


def test_a1_full_replay_holds_only_the_trending_coin_within_the_vol_budget():
    streams = _streams(600, {"BTCUSDT": 0.0, "ETHUSDT": 0.004, "XRPUSDT": -0.004})
    strat = DonchianEnsemble(min_median_volume=0.0)                   # synthetic volume is tiny
    acct = PaperAccount(1000, 3, CostModel(0, 0, 0))
    replay(streams, [strat], {"net": acct})
    assert "ETHUSDT" in acct.positions and "XRPUSDT" not in acct.positions
    eth = acct.positions["ETHUSDT"]["qty"] * acct.last["ETHUSDT"]
    assert eth <= acct.equity() / 3 + 1e-6                             # never above its 1/K share


def test_a1_young_or_illiquid_coins_are_not_traded():
    streams = _streams(420, {"BTCUSDT": 0.003, "ETHUSDT": 0.003})
    acct = PaperAccount(1000, 2, CostModel(0, 0, 0))
    replay(streams, [DonchianEnsemble()], {"net": acct})               # default $2M median volume: none pass
    assert acct.positions == {} and acct.fills == 0


# ----- A3 ----------------------------------------------------------------------------------------------
def test_a3_holds_rising_majors_in_equal_weight_and_cash_otherwise():
    streams = _streams(200, {"BTCUSDT": 0.003, "ETHUSDT": 0.003, "XRPUSDT": -0.004}, vol=0.005)
    acct = PaperAccount(1000, 7, CostModel(0, 0, 0))
    replay(streams, [TsmomRiskManaged()], {"net": acct})
    assert set(acct.positions) == {"BTCUSDT", "ETHUSDT"}
    vals = [p["qty"] * acct.last[s] for s, p in acct.positions.items()]
    assert vals[0] == pytest.approx(vals[1], rel=0.02)                # rebalanced daily to equal weight


def test_a3_halves_exposure_in_a_market_drawdown():
    d0 = ms("2019-01-07T00:00:00")
    up = list(np.linspace(100, 200, 120))
    crash = list(np.linspace(200, 140, 10)) + list(np.linspace(140, 168, 35))   # -30%, then a 30d rebound to -16%
    streams = {s: daily_minutes(s, d0, up + crash, per_day=2) for s in ("BTCUSDT", "ETHUSDT")}
    strat = TsmomRiskManaged()
    acct = PaperAccount(1000, 7, CostModel(0, 0, 0))
    replay(streams, [strat], {"net": acct})
    gross = sum(strat._weights.values())
    assert strat._index < 0.85 * strat._peak and gross == pytest.approx(0.5)


def test_both_are_registered_in_the_trend_family():
    from trading.replay.gate import family_of
    for name in ("donchian_ensemble", "tsmom_risk_managed"):
        assert DEFAULT.get(name).family == family_of(name) == "trend"
