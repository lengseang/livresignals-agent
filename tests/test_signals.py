"""P2-01 tests: indicator golden values, no-lookahead, regime hysteresis."""
from __future__ import annotations

import math

import pytest

from trading.signals.indicators import adx, atr, ema, realized_vol
from trading.signals.regime import classify_regime, regime_for_rows


def _rows(closes: list[float], highs: list[float] | None = None, lows: list[float] | None = None) -> list[dict]:
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    return [
        {"open": c, "high": h, "low": l, "close": c, "open_time_utc": i * 3600_000}
        for i, (c, h, l) in enumerate(zip(closes, highs, lows))
    ]


def test_ema_golden():
    # Hand-computed EMA(3) on closes 1..5: k=0.5
    closes = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = ema(closes, 3)
    # seed = (1+2+3)/3 = 2.0
    assert len(out) == 3
    assert out[0] == pytest.approx(2.0, abs=1e-9)
    assert out[1] == pytest.approx(0.5 * 4.0 + 0.5 * 2.0, abs=1e-9)  # 3.0
    assert out[2] == pytest.approx(0.5 * 5.0 + 0.5 * 3.0, abs=1e-9)  # 4.0


def test_atr_golden_small():
    # ATR(3) on flat highs/lows; TR = high-low each bar.
    rows = _rows(
        closes=[10, 11, 12, 13, 14],
        highs=[12, 13, 14, 15, 16],
        lows=[8, 9, 10, 11, 12],
    )
    out = atr(rows, 3)
    # TRs all = 4; seed = 4; smoothed = 4
    assert out == [4.0, 4.0, 4.0]


def test_adx_needs_warmup():
    rows = _rows([float(i) for i in range(20)])
    assert adx(rows, 14) == []  # needs 2*14 rows


def test_realized_vol_golden():
    # Constant closes -> zero returns -> zero vol.
    rows = _rows([100.0] * 30)
    out = realized_vol(rows, 20)
    assert out and out[-1] == pytest.approx(0.0, abs=1e-12)


def test_no_lookahead():
    """A future candle must not change today's indicator values."""
    closes = [float(i) for i in range(100)]
    rows = _rows(closes)
    a_before = atr(rows[:70], 14)[-1]
    a_after = atr(rows[:71], 14)[-2]  # value at same index after one more row
    assert a_before == pytest.approx(a_after, abs=1e-12)


def test_regime_insufficient_data():
    rows = _rows([float(i) for i in range(30)])
    assert classify_regime(rows) == "RANGING"  # safest default


def test_regime_trending_strong_up():
    # Monotonic strong up move: ADX high, EMA separation large.
    closes = [100.0 * (1.01 ** i) for i in range(120)]
    rows = _rows(closes)
    reg = classify_regime(rows)
    assert reg == "TRENDING"


def test_regime_hysteresis_prevents_flap():
    # Force a regime that would differ from prev only on the last candle.
    closes = [100.0 * (1.01 ** i) for i in range(120)]
    rows = _rows(closes)
    first = regime_for_rows(rows, None)
    # Feeding the same rows with a mismatched prev should NOT switch instantly.
    stable = regime_for_rows(rows, first)
    assert stable == first


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))