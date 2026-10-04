"""The exit-study simulator is only evidence if its mechanics are right: no look-ahead in the ratchet, stop-first
when both levels are touched, gap fills, monotone stops, costs on both sides."""
import numpy as np
import pytest

from trading.v2.studies.exit_study import (
    FEE, SIDE_COST, STOP, TARGET, Rule, simulate_trade, stop_path,
)

BR = Rule("R1_bracket")
LOCK = Rule("lock", lock_at=0.5)


def bars(rows):
    a = np.array(rows, dtype=float)
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3], np.full(len(a), 1.0)   # o, h, l, c, atr_px


def net(entry_mid, exit_px):
    return exit_px * (1 - SIDE_COST) * (1 - FEE) / (entry_mid * (1 + SIDE_COST) * (1 + FEE)) - 1


def test_bracket_hits_target_at_target_price():
    ef = 100 * (1 + SIDE_COST)
    o, h, l, c, a = bars([(100, 101, 99, 100), (100, ef * 1.08, 100, 107)])
    r, mfe, why, i = simulate_trade(BR, o, h, l, c, a)
    assert (why, i) == ("target", 1)
    assert r == pytest.approx(net(100, ef * (1 + TARGET)))


def test_stop_first_when_both_touched_in_one_bar():
    ef = 100 * (1 + SIDE_COST)
    o, h, l, c, a = bars([(100, ef * 1.2, ef * 0.9, 100)])
    assert simulate_trade(BR, o, h, l, c, a)[2] == "stop"


def test_gap_through_stop_fills_at_open_not_stop():
    ef = 100 * (1 + SIDE_COST)
    o, h, l, c, a = bars([(100, 100.5, 99.5, 100), (90, 91, 89, 90)])
    r, _, why, _ = simulate_trade(BR, o, h, l, c, a)
    assert why == "stop" and r == pytest.approx(net(100, 90))
    assert 90 < ef * (1 - STOP)


def test_ratchet_uses_previous_bar_high_only():
    # bar 1 spikes to +6% and dumps to +1% inside the SAME bar: the lock computed from that high
    # may only apply from bar 2 on, so bar 1 must not be stopped by it
    ef = 100 * (1 + SIDE_COST)
    o, h, l, c, a = bars([(100, 100, 100, 100), (100, ef * 1.06, ef * 1.01, ef * 1.02), (ef * 1.02, ef * 1.02, ef * 1.0, ef * 1.0)])
    s = stop_path(LOCK, ef, h, a)
    assert s[1] == pytest.approx(ef * (1 - STOP))
    assert s[2] == pytest.approx(ef * 1.03)          # entry + 0.5 * 6% MFE
    r, mfe, why, i = simulate_trade(LOCK, o, h, l, c, a)
    assert (why, i) == ("stop", 2)
    assert r > 0                                        # the owner's scenario: a near-target trade keeps a profit


def test_stops_never_loosen_property():
    rng = np.random.default_rng(3)
    for rule in (LOCK, Rule("be", be_at=0.5), Rule("tr", trail_at=0.25, trail_k=2), Rule("ch", target=False, trail_at=0.0, trail_k=3)):
        for _ in range(50):
            steps = rng.normal(0, 0.004, 300)
            c = 100 * np.exp(np.cumsum(steps))
            h = c * (1 + np.abs(rng.normal(0, 0.002, 300)))
            atr = np.abs(rng.normal(1.0, 0.5, 300))
            s = stop_path(rule, 100.0, h, atr)
            assert np.all(np.diff(s) >= 0)
            assert s[0] == pytest.approx(100.0 * (1 - STOP))


def test_stop_only_rule_ignores_target():
    ef = 100 * (1 + SIDE_COST)
    o, h, l, c, a = bars([(100, ef * 1.2, 100, ef * 1.1), (ef * 1.1, ef * 1.1, ef * 1.09, ef * 1.1)])
    assert simulate_trade(Rule("R0", target=False), o, h, l, c, a)[2] == "timeout"
