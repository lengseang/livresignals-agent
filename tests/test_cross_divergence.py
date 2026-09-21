"""Cross-divergence detector tests."""
from __future__ import annotations

import pytest

from trading.signals import multi_signal as ms


def test_divergence_fires_on_laggards():
    moves = {
        "BTC/USDT": {"move_24h": 0.025},
        "ETH/USDT": {"move_24h": 0.001},
        "SOL/USDT": {"move_24h": 0.002},
    }
    out = ms.detect_cross_divergence(moves)
    assert len(out) == 2
    syms = {s["symbol"] for s in out}
    assert syms == {"ETH/USDT", "SOL/USDT"}
    assert all(s["signal_type"] == "cross_div" for s in out)
    assert all(s["severity"] == "SMALL" for s in out)


def test_divergence_silent_when_aligned():
    moves = {
        "BTC/USDT": {"move_24h": 0.035},
        "ETH/USDT": {"move_24h": 0.053},
        "SOL/USDT": {"move_24h": 0.037},
    }
    assert ms.detect_cross_divergence(moves) == []


def test_divergence_silent_when_btc_flat():
    moves = {
        "BTC/USDT": {"move_24h": 0.002},
        "ETH/USDT": {"move_24h": 0.001},
        "SOL/USDT": {"move_24h": 0.002},
    }
    assert ms.detect_cross_divergence(moves) == []


def test_divergence_down_direction():
    moves = {
        "BTC/USDT": {"move_24h": -0.02},
        "ETH/USDT": {"move_24h": -0.001},
        "SOL/USDT": {"move_24h": -0.002},
    }
    out = ms.detect_cross_divergence(moves)
    assert len(out) == 2
    assert all("catch-up down" in s["reason"] for s in out)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))