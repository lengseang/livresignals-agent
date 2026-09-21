"""P2-02 tests: scorer golden values, fail-closed, config-driven weights."""
from __future__ import annotations

import json

import pytest

from trading.signals.scorer import DEFAULT_WEIGHTS, compute_score, decision_record


def _rows(closes: list[float]) -> list[dict]:
    return [
        {"open": c, "high": c * 1.005, "low": c * 0.995, "close": c, "open_time_utc": i * 3600_000}
        for i, c in enumerate(closes)
    ]


def test_score_neutral_flat_market():
    closes = [100.0 + (i % 3) * 0.1 for i in range(100)]
    result = compute_score(_rows(closes))
    assert result["score"] is not None
    assert 40 <= result["score"] <= 60  # flat market near neutral


def test_score_bullish_trend():
    closes = [100.0 * (1.005 ** i) for i in range(120)]
    result = compute_score(_rows(closes))
    assert result["score"] is not None
    assert result["score"] > 60  # strong up trend -> bullish tilt


def test_score_never_exceeds_bounds():
    closes = [100.0 * (1.02 ** i) for i in range(120)]
    result = compute_score(_rows(closes))
    assert result["score"] is not None
    assert 0 <= result["score"] <= 100


def test_fail_closed_insufficient_rows():
    result = compute_score(_rows([100.0] * 30))
    assert result["score"] is None  # NO_TRADE
    assert "insufficient_rows" in result["missing"]


def test_fail_closed_missing_indicator():
    # Realistic: rows without enough history for ADX(14) warmup.
    closes = [100.0 + i for i in range(25)]
    result = compute_score(_rows(closes))
    assert result["score"] is None  # NO_TRADE, never 0


def test_weights_config_driven(tmp_path):
    from trading.signals import scorer
    cfg = tmp_path / "scorer-weights.json"
    custom = {
        "RANGING": {"momentum": 0.5, "trend_strength": 0.2, "mean_reversion": 0.3, "vol_dampening": 0.0},
        "TRENDING": {"momentum": 0.6, "trend_strength": 0.3, "mean_reversion": 0.0, "vol_dampening": 0.1},
        "VOLATILE": {"momentum": 0.3, "trend_strength": 0.2, "mean_reversion": 0.1, "vol_dampening": 0.4},
    }
    cfg.write_text(json.dumps(custom), encoding="utf-8")
    old = scorer.WEIGHTS_PATH
    scorer.WEIGHTS_PATH = cfg
    try:
        loaded = scorer.load_weights()
        assert loaded["TRENDING"]["momentum"] == 0.6
    finally:
        scorer.WEIGHTS_PATH = old


def test_decision_record_shape():
    closes = [100.0 * (1.005 ** i) for i in range(120)]
    rec = decision_record("BTC/USDT", "1h", _rows(closes))
    assert rec["symbol"] == "BTC/USDT"
    assert rec["score"] is not None
    assert rec["macro_multiplier"] == 1.0  # frozen (no unique-traders source)
    assert rec["action"] == "PENDING_RISK"


def test_default_weights_total_one():
    for regime, w in DEFAULT_WEIGHTS.items():
        assert abs(sum(w.values()) - 1.0) < 1e-9


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))