"""Calibration rules, pinned exactly on a controllable fake feed. No network."""
import dataclasses

import pytest

from trading.replay.bars import Bar
from trading.v2.calibrate import Calibration, calibrate, to_runner_config, _up
from trading.v2.features import Book

NOW = 1_700_000_000_000
M, H = 60_000, 3_600_000


class Feed:
    """closes_1m[sym]: list of 1m closes (served once, then empty); atr_pct[sym]: constant (H-L)/close of the 1h bars;
    spreads[sym]: successive book spreads in bps."""

    def __init__(self, closes_1m, atr_pct, spreads):
        self.c1, self.atr, self.sp = closes_1m, atr_pct, {k: list(v) for k, v in spreads.items()}
        self.served = set()

    def now_ms(self):
        return NOW

    def klines(self, sym, interval, limit=500, end_time_ms=None, now_ms=None):
        if (sym, end_time_ms) in self.served:
            return []
        self.served.add((sym, end_time_ms))
        cs = self.c1[sym]
        return [Bar(sym, "1m", NOW - (len(cs) - i) * M, NOW - (len(cs) - i - 1) * M, c, c, c, c, 1, c, 1, 1)
                for i, c in enumerate(cs)]

    def history(self, sym, interval, n, now_ms=None):
        r = self.atr[sym]
        return [Bar(sym, "1h", i * H, (i + 1) * H, 100.0, 100.0 + r * 100 / 2, 100.0 - r * 100 / 2, 100.0, 1, 1, 1, 1)
                for i in range(n)]

    def book(self, sym):
        s = self.sp[sym].pop(0) if len(self.sp[sym]) > 1 else self.sp[sym][0]
        mid = 100.0
        return Book(NOW, mid - mid * s / 2e4, mid + mid * s / 2e4, 1, 1)


def cal(closes=(100.0, 100.5, 100.0), atr=0.005, spreads=(1.0,), **kw):
    f = Feed({"AAAUSDT": list(closes)}, {"AAAUSDT": atr}, {"AAAUSDT": list(spreads)})
    return calibrate(f, ("AAAUSDT",), sleep=lambda s: None, spread_samples=len(spreads), one_min_pages=1, **kw)


def test_round_up_helper():
    assert _up(0.0151, 0.005) == 0.02 and _up(0.015, 0.005) == 0.015 and _up(0.016, 0.005) == 0.02 and _up(4.2, 1.0) == 5.0


@pytest.mark.parametrize("closes,expected", [
    ((100.0, 100.5, 100.0), 0.02),             # max move 0.5%: 3x = 1.5% -> clamped UP to the 2% floor
    ((100.0, 102.0, 100.0), 0.06),             # 2% move -> 3x = 6%
    ((100.0, 110.0), 0.15),                    # 10% -> 30% -> clamped to the 15% ceiling
    ((100.0, 101.0), 0.03),                    # 1% -> 3% exactly
])
def test_kill_price_move_is_three_times_the_largest_one_minute_move_clamped(closes, expected):
    c = cal(closes=closes)
    assert c.kill_max_price_move_frac == pytest.approx(expected)
    assert c.max_1m_move == pytest.approx(max(abs(b / a - 1) for a, b in zip(closes, closes[1:])))


@pytest.mark.parametrize("atr,stop", [(0.001, 0.01), (0.007, 0.025), (0.0114, 0.035), (0.05, 0.05)])
def test_stop_is_three_times_the_p90_hourly_atr_clamped_to_one_to_five_percent(atr, stop):
    c = cal(atr=atr)
    assert c.stop_pct == pytest.approx(stop) and c.target_pct == pytest.approx(2 * stop)
    assert c.atr1h_pct_p90["AAAUSDT"] == pytest.approx(atr, rel=1e-6)


def test_the_stop_never_exceeds_the_legacy_maximum_stop_distance():
    from trading.risk.guards import GUARDS
    for atr in (0.0005, 0.01, 0.03, 0.2):
        assert cal(atr=atr).stop_pct <= GUARDS.max_stop_distance


@pytest.mark.parametrize("spreads,median,max_spread", [((1.0, 2.0, 9.0), 2.0, 8.0), ((3.0,), 3.0, 12.0), ((0.1, 0.1), 0.1, 5.0), ((1.0,), 1.0, 5.0)])
def test_spread_is_the_sample_median_and_the_cap_never_drops_below_five_bps(spreads, median, max_spread):
    c = cal(spreads=spreads)
    assert c.median_spread_bps["AAAUSDT"] == pytest.approx(median, rel=1e-6)
    assert c.paper_spread_bps == pytest.approx(median, abs=1e-4) and c.risk_max_spread_bps == max_spread


def test_the_worst_symbol_sets_the_shared_numbers():
    f = Feed({"AAAUSDT": [100.0, 100.2], "BBBUSDT": [100.0, 101.5]}, {"AAAUSDT": 0.002, "BBBUSDT": 0.009},
             {"AAAUSDT": [0.5], "BBBUSDT": [4.0]})
    c = calibrate(f, ("AAAUSDT", "BBBUSDT"), sleep=lambda s: None, spread_samples=1, one_min_pages=1)
    assert c.max_1m_move == pytest.approx(0.015) and c.kill_max_price_move_frac == pytest.approx(0.045)
    assert c.stop_pct == pytest.approx(0.03) and c.paper_spread_bps == pytest.approx(4.0, abs=1e-4)
    assert c.risk_max_spread_bps == 16.0


def test_dollar_limits_scale_with_equity():
    c = cal(equity=5_000.0)
    assert (c.daily_loss_cap_usd, c.kill_daily_loss_cap_usd, c.daily_loss_budget_usd) == (100.0, 200.0, 100.0)
    assert cal().daily_loss_cap_usd == 20.0 and cal().kill_daily_loss_cap_usd == 2 * cal().daily_loss_cap_usd


def test_the_proposal_converts_to_a_valid_paper_runner_config(tmp_path):
    c = cal(closes=(100.0, 101.0), atr=0.0114, spreads=(1.0,))
    cfg = to_runner_config(c, 15, tmp_path)
    assert cfg.exit_params.stop_pct == pytest.approx(0.035) and cfg.exit_params.target_pct == pytest.approx(0.07)
    assert cfg.kill_limits.max_price_move_frac == pytest.approx(0.03) and cfg.kill_limits.daily_loss_cap_usd == 40.0
    assert cfg.risk_limits.daily_loss_cap_usd == 20.0 and cfg.daily_loss_budget_usd == 20.0
    assert cfg.costs.fee_rate == 0.001 and cfg.costs.spread_bps == pytest.approx(1.0, abs=1e-4) and cfg.costs.slippage_bps == 1.0
    assert cfg.kill_limits.min_request_confidence == 0.8 and cfg.cadence_minutes == 15
    # the kill switch is looser than the risk cap (it is the emergency stop), and the risk cap is tighter than the legacy 5% halt
    assert cfg.kill_limits.daily_loss_cap_usd > cfg.risk_limits.daily_loss_cap_usd
    assert cfg.risk_limits.daily_loss_cap_usd / cfg.paper_equity < 0.05


def test_calibration_is_deterministic_and_reports_its_evidence():
    a, b = cal(closes=(100.0, 101.0, 100.4)), cal(closes=(100.0, 101.0, 100.4))
    assert a == b and a.one_min_bars == 3 and dataclasses.is_dataclass(a) and isinstance(a, Calibration)
