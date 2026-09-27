"""P1c: target-weight orders in the replay broker (docs/loop/designs/P1c-target-weights.md)."""
from __future__ import annotations

import pytest

from trading.replay.bars import Bar
from trading.replay.broker import CostModel, PaperAccount
from trading.replay.strategy import Signal

T0 = 1_700_000_000_000


def bar(sym: str, t: int, px: float) -> Bar:
    return Bar(sym, "1m", t, t + 60_000, px, px, px, px, 1.0, px, 1, 0.5)


def target(sym: str, w: float, t: int = T0) -> Signal:
    return Signal("test", sym, "target", t, "rebalance test", 0.0, "1d", weight=w)


def fill(acct: PaperAccount, bars: list[Bar]) -> None:
    for action in ("close", "open"):                    # the engine's order: every sale, then every purchase
        for b in bars:
            acct.on_open(b, action)


def test_buys_up_to_the_target_weight():
    a = PaperAccount(1000, 1, CostModel(0, 0, 0))
    a.submit(target("BTC", 0.5))
    fill(a, [bar("BTC", T0 + 1, 100.0)])
    assert a.positions["BTC"]["qty"] == pytest.approx(5.0) and a.cash == pytest.approx(500.0)


def test_trims_part_of_a_position_and_books_it_as_a_trade():
    a = PaperAccount(1000, 1, CostModel(0, 0, 0))
    a.submit(target("BTC", 0.5))
    fill(a, [bar("BTC", T0 + 1, 100.0)])
    a.submit(target("BTC", 0.25, T0 + 10))
    fill(a, [bar("BTC", T0 + 11, 200.0)])               # equity 1500 -> hold 375 of BTC: sell 3.125
    assert a.positions["BTC"]["qty"] == pytest.approx(1.875)
    t = a.trades[-1]
    assert t["qty"] == pytest.approx(3.125) and t["net_ret"] == pytest.approx(1.0)   # bought 100, sold 200
    assert a.equity() == pytest.approx(1500.0)


def test_weight_zero_is_an_ordinary_full_close():
    a = PaperAccount(1000, 1, CostModel(0, 0, 0))
    a.submit(target("BTC", 1.0))
    fill(a, [bar("BTC", T0 + 1, 100.0)])
    a.submit(target("BTC", 0.0, T0 + 10))
    fill(a, [bar("BTC", T0 + 11, 110.0)])
    assert "BTC" not in a.positions and a.cash == pytest.approx(1100.0)


def test_spot_only_purchases_are_capped_by_cash():
    a = PaperAccount(1000, 1, CostModel(0, 0, 0))
    a.submit(target("BTC", 0.8))
    a.submit(target("ETH", 0.8))
    fill(a, [bar("BTC", T0 + 1, 100.0), bar("ETH", T0 + 1, 10.0)])
    assert a.cash == pytest.approx(0.0) and a.positions["ETH"]["qty"] * 10 == pytest.approx(200.0)


def test_sales_fill_before_purchases_at_the_same_instant():
    a = PaperAccount(1000, 1, CostModel(0, 0, 0))
    a.submit(target("BTC", 1.0))
    fill(a, [bar("BTC", T0 + 1, 100.0)])
    a.submit(target("ETH", 1.0, T0 + 10))               # ETH listed first: its purchase must still wait
    a.submit(target("BTC", 0.0, T0 + 10))
    fill(a, [bar("ETH", T0 + 11, 10.0), bar("BTC", T0 + 11, 100.0)])
    assert "BTC" not in a.positions and a.positions["ETH"]["qty"] == pytest.approx(100.0)


def test_costs_are_charged_on_the_traded_amount_only():
    a = PaperAccount(1000, 1, CostModel(0.001, 0, 0))
    a.submit(target("BTC", 0.5))
    fill(a, [bar("BTC", T0 + 1, 100.0)])
    fee_first = a.positions["BTC"]["fee"]
    a.submit(target("BTC", 0.55, T0 + 10))
    fill(a, [bar("BTC", T0 + 11, 100.0)])
    added = a.positions["BTC"]["fee"] - fee_first
    assert added == pytest.approx(fee_first * (0.05 / 0.5), rel=0.02)   # ~10% of the first trade's size


def test_open_close_signals_record_no_weight_field():
    s = Signal("x", "BTC", "open", T0, "r", 1.0, "1d")
    assert "weight" not in s.record("REPLAY")["payload"]
    assert s.weight is None
