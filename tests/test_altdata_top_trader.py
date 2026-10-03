"""Tests for altdata_logger top-trader positioning capture (added 2026-10-02).

Pure-function tests — no network. Guarantees a partial snapshot never raises and
that the retail-vs-whale divergence is computed only when both inputs exist.
"""
import importlib.util
from pathlib import Path

import pytest


def _load():
    spec = importlib.util.spec_from_file_location(
        "altdata_logger",
        Path(__file__).resolve().parents[1] / "scripts" / "altdata_logger.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


alt = _load()


def _pos(lsr, long_acc):
    return {"longShortRatio": str(lsr), "longAccount": str(long_acc)}


def test_divergence_positive_when_whales_longer():
    row = {"ls_ratio": 1.939}
    out = alt.apply_top_trader_positioning(row, _pos(2.218, 0.689), _pos(1.83, 0.65))
    assert out["top_pos_lsr"] == pytest.approx(2.218)
    assert out["top_pos_long_pct"] == pytest.approx(68.9)
    assert out["top_acct_lsr"] == pytest.approx(1.83)
    assert out["lsr_divergence"] == pytest.approx(0.279, abs=1e-3)


def test_divergence_negative_when_whales_shorter():
    row = {"ls_ratio": 2.50}
    out = alt.apply_top_trader_positioning(row, _pos(1.10, 0.52), None)
    assert out["lsr_divergence"] == pytest.approx(-1.40)
    assert "top_acct_lsr" not in out


def test_no_divergence_without_retail_ratio():
    row = {}
    out = alt.apply_top_trader_positioning(row, _pos(2.0, 0.66), _pos(1.9, 0.65))
    assert "lsr_divergence" not in out
    assert out["top_pos_lsr"] == 2.0


def test_missing_data_never_raises():
    row = {"ls_ratio": 1.5}
    out = alt.apply_top_trader_positioning(row, None, None)
    assert out == {"ls_ratio": 1.5}          # untouched, no exception
    assert "top_pos_lsr" not in out


def test_empty_dict_is_treated_as_missing():
    # Binance can return [] -> g_last maps that to None, but be defensive: an
    # empty dict is falsy and must not be written as a bogus 0.0 ratio.
    row = {"ls_ratio": 1.2}
    out = alt.apply_top_trader_positioning(row, {}, {})
    assert "top_pos_lsr" not in out
    assert "lsr_divergence" not in out
