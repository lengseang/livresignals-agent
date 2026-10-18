"""The regime label must be what the strategy's name says it is.

Regression tests for the defect found on 2026-10-17:

  regime_trader.py's docstring says "BULL (BTC > SMA200) / BEAR (BTC < SMA200)"
  and the live log prints "regime=BULL (BTC 81472 vs SMA200 72430)". But the
  label is computed from `SMA_REGIME`, which `champion.json` set to 200 on
  2026-09-22 overnight -- and `champion.py`'s variant grid (sma_r 200/100/150/
  120) keeps churning that key. Today the label is right by luck of the last
  promotion, not by construction: while it read 150, BTC was 12% ABOVE the
  level the strategy actually used and the log would still have said "BULL
  (BTC x vs SMA150 y)". Worse for a BEAR call: the whole short leg only arms
  when BTC is below the regime SMA, so a tunable value silently changes the
  regime definition the long/short switch depends on.

The fix: the entry/exit RULE keeps using the champion-tunable `SMA_REGIME`
(measured, unchanged), but the LABEL is computed against the constant the
docstring names (`LABEL_SMA = 200`) and the divergence is printed whenever the
two disagree, so a regime call can never claim a level it did not use.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(scope="module")
def rt():
    spec = importlib.util.spec_from_file_location(
        "regime_trader_under_test", ROOT / "scripts" / "regime_trader.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["regime_trader_under_test"] = m
    spec.loader.exec_module(m)
    return m


def test_label_sma_is_the_documented_200(rt):
    assert rt.LABEL_SMA == 200


def test_label_uses_documented_sma_not_the_tuned_one(rt):
    """Close above SMA200 but below SMA150 => documented label BULL, rule says BEAR."""
    btc = [100.0] * 150 + [140.0] * 50 + [112.0] * 5
    lab = rt.regime_label(btc, 150)
    assert lab["regime"] == "BULL"
    assert lab["label_sma"] == 200
    assert lab["diverges"] is True          # rule SMA (150) disagrees
    assert rt.regime_label_for_rule(btc, 150)["regime"] == "BEAR"


def test_agreeing_config_reports_no_divergence(rt):
    btc = [100.0] * 250
    lab = rt.regime_label(btc, 200)
    assert lab["regime"] == "BULL"
    assert lab["diverges"] is False


def test_bear_label_uses_documented_sma(rt):
    """Close below both SMAs => BEAR on both, no divergence (the common case)."""
    btc = [100.0] * 200 + [60.0] * 10
    lab = rt.regime_label(btc, 150)
    assert lab["regime"] == "BEAR"
    assert lab["diverges"] is False
    # ...and the opposite divergence: rule SMA200 BEAR while SMA150 is far
    # lower (long downtrend still rolling out of the 200 window).
    d = [200.0 - i for i in range(210)]
    lab2 = rt.regime_label(d, 200)
    if lab2["diverges"]:
        assert lab2["regime"] != rt.regime_label_for_rule(d, 200)["regime"]


def test_main_label_matches_the_rule_it_trades(rt):
    """Whatever `SMA_REGIME` the champion file sets, the *rule* label must be
    the boolean that arms the short leg.

    The first draft of the fix made main() call `regime_label()` (the
    DOCUMENTED SMA200 label) and print `lab["regime"]`, while `bear` -- which
    arms the short leg and gates long entries -- still came from SMA_REGIME.
    A test that only checked "label == f(bear)" would have passed, because it
    would have checked the same wrong source twice. So this also asserts the
    two are the SAME object-level decision, and the test below pins the call
    site in main()'s source.
    """
    btc = [150.0] * 120 + [100.0 - i * 0.6 for i in range(60)] + [64.0 + i for i in range(30)]
    for sma_rule in (100, 120, 150, 200):
        arms_short = btc[-1] < rt.sma(btc, sma_rule)
        lab = rt.regime_label_for_rule(btc, sma_rule)
        assert lab["regime"] == ("BEAR" if arms_short else "BULL")
        # The documented label is a DIFFERENT question (SMA200) and must stay
        # independent of the rule SMA -- otherwise the two functions are one.
        assert rt.regime_label(btc, sma_rule)["label_sma"] == 200


def test_main_source_does_not_call_the_documented_label(rt):
    """Belt and braces: main() must not call regime_label() for its `regime`."""
    src = (ROOT / "scripts" / "regime_trader.py").read_text()
    body = src.split("def main(", 1)[1]
    assert "regime_label_for_rule(btc" in body
    assert "lab = regime_label(" not in body
