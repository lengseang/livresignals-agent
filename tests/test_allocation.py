"""P-A tests: scaling rules, benchmarks, guard integration."""
from __future__ import annotations

import pytest

from trading.allocation.benchmark import buy_and_hold, compare_strategies, plain_dca
from trading.allocation.engine import (
    Accumulator,
    AllocationConfig,
    AllocationState,
    run_accumulation,
    scaling_factor,
)


def _closes(series: list[float]) -> list[float]:
    return [float(x) for x in series]


def test_scaling_factor_dip():
    cfg = AllocationConfig(drawdown_threshold=-0.15, dip_multiplier=2.0)
    # price 85 vs 90-day high 100 -> dd -15% -> 2x
    lookback = [100.0] * 90
    assert scaling_factor(85.0, lookback, cfg) == 2.0
    # price 99 -> dd -1% -> 1x
    assert scaling_factor(99.0, lookback, cfg) == 1.0


def test_scaling_factor_extension():
    cfg = AllocationConfig(extension_threshold=0.15, extension_multiplier=0.5)
    # price 116 vs 90-day low 100 -> +16% -> 0.5x
    lookback = [100.0] * 90
    assert scaling_factor(116.0, lookback, cfg) == 0.5


def test_scaling_factor_empty_lookback():
    assert scaling_factor(100.0, [], AllocationConfig()) == 1.0


def test_accumulator_monthly_cadence():
    cfg = AllocationConfig(base_amount=100.0, interval_days=30, lookback_days=30)
    closes = _closes([100.0 + i * 0.5 for i in range(100)])
    acc = Accumulator(cfg)
    records = []
    for i in range(len(closes)):
        r = acc.step(closes, i)
        if r and not r["paused"]:
            records.append(r)
    # monthly for ~70 eligible candles -> ~2-3 entries
    assert len(records) >= 2
    assert acc.state.entries == len(records)


def test_accumulator_breaker_pauses():
    cfg = AllocationConfig(base_amount=100.0, interval_days=10, lookback_days=30)
    closes = _closes([100.0 + i for i in range(60)])
    acc = Accumulator(cfg)
    # Simulate a deep drawdown: equity 80 vs HWM 100 -> -20% -> paused
    paused = acc.check_breakers(equity=80.0, day_start_equity=100.0, high_water_mark=100.0)
    assert paused is True
    # next entry attempt should pause, not buy
    r = acc.step(closes, 40)
    assert r is not None and r["paused"] is True


def test_compare_strategies_scaled_beats_plain_on_dip_series():
    # A series with a deep dip: scaled should buy more BTC per dollar.
    closes = _closes([100.0] * 60 + [70.0] * 30 + [130.0] * 30)
    cfg = AllocationConfig(base_amount=100.0, interval_days=15, lookback_days=60,
                           drawdown_threshold=-0.15, dip_multiplier=2.0)
    result = compare_strategies(closes, cfg)
    # scaled invests 2x during the dip -> more BTC
    assert result["scaled"]["btc"] > result["plain"]["btc"]


def test_plain_dca_and_buy_hold_sane():
    closes = _closes([100.0 + i for i in range(100)])
    d = plain_dca(closes, AllocationConfig(base_amount=10.0, interval_days=20, lookback_days=20))
    assert d["cash_invested"] > 0 and d["btc"] > 0
    bh = buy_and_hold(closes)
    assert bh["btc"] > 0 and bh["entries"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))