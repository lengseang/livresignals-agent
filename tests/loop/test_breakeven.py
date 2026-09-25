"""The break-even formula behind docs/research/fees-and-breakeven.md."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "breakeven", Path(__file__).resolve().parents[2] / "research" / "07-breakeven.py"
)
be = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(be)


def test_zero_cost_symmetric_trade_breaks_even_at_half():
    assert be.breakeven_win_rate(0.01, 0.01, 0.0) == pytest.approx(0.5)


def test_expectancy_is_zero_at_the_breakeven_rate():
    tp, sl, c = 0.003, 0.003, be.round_trip_cost(0.001, 0.0002)
    p = be.breakeven_win_rate(tp, sl, c)
    assert p * (tp - c) - (1 - p) * (sl + c) == pytest.approx(0.0, abs=1e-15)


def test_costs_raise_the_bar():
    free = be.breakeven_win_rate(0.003, 0.003, 0.0)
    costly = be.breakeven_win_rate(0.003, 0.003, be.round_trip_cost(0.001, 0.0))
    assert costly > free
