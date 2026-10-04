"""V2-15 L0/L1: take profit, MFE/MAE, and the lock ratchet, in the rule and in the paper book."""
import numpy as np
import pytest

from trading.v2.paper import PaperBook
from trading.v2.position import RatchetRule, ratchet

from test_v2_paper import M, T0, ZERO, b, cmd, short_cmd

RULE = RatchetRule()           # the study's defaults: arm at 50% of target, lock 50% of MFE


def long_book(rule=RULE, stop=95.0, target=110.0):
    bk = PaperBook(1000.0, ZERO, None, ratchet=rule)
    bk.submit(cmd(size=1.0, stop_price=stop, target_price=target), T0)
    bk.on_bar(b(0, 100.0))
    return bk


# ------------------------------------------------------------ the pure rule

def test_rule_does_not_arm_before_half_target():
    assert ratchet(RULE, side="LONG", entry=100, target=110, best=104.9, stop=95, last=104) is None


def test_rule_locks_half_the_excursion():
    a = ratchet(RULE, side="LONG", entry=100, target=110, best=106, stop=95, last=105)
    assert (a.kind, a.level) == ("tighten", pytest.approx(103.0))


def test_rule_exit_when_lock_is_through_the_last_price():
    a = ratchet(RULE, side="LONG", entry=100, target=110, best=108, stop=95, last=103.5)
    assert (a.kind, a.level) == ("exit", pytest.approx(104.0))


def test_rule_short_is_symmetric():
    a = ratchet(RULE, side="SHORT", entry=100, target=90, best=94, stop=105, last=95)
    assert (a.kind, a.level) == ("tighten", pytest.approx(97.0))


def test_rule_never_loosens_and_needs_a_target():
    assert ratchet(RULE, side="LONG", entry=100, target=110, best=106, stop=103.5, last=105) is None
    assert ratchet(RULE, side="LONG", entry=100, target=None, best=150, stop=95, last=149) is None


@pytest.mark.parametrize("bad", [dict(activate_frac=0), dict(activate_frac=1.5), dict(lock_frac=1.0), dict(lock_frac=0)])
def test_rule_parameters_validated(bad):
    with pytest.raises(ValueError):
        RatchetRule(**bad)


def test_rule_property_never_loosens_never_crosses():
    rng = np.random.default_rng(5)
    for _ in range(2000):
        side = rng.choice(["LONG", "SHORT"])
        s = 1 if side == "LONG" else -1
        entry = 100.0
        target = entry * (1 + s * rng.uniform(0.01, 0.1))
        stop = entry * (1 - s * rng.uniform(0.005, 0.05))
        best = entry * (1 + s * rng.uniform(0, 0.12))
        last = entry * (1 + s * rng.uniform(-0.05, 0.12))
        a = ratchet(RULE, side=side, entry=entry, target=target, best=best, stop=stop, last=last)
        if a is None:
            continue
        assert s * (a.level - stop) > 0                         # tighter, always
        assert (a.kind == "tighten") == (s * (last - a.level) > 0)


# ------------------------------------------------------------ L0: take profit in the paper book

def test_target_fills_at_target():
    bk = long_book(rule=None)
    bk.on_bar(b(1, 100.0, 111.0, 99.5, 108.0))
    assert bk.trades[-1]["reason"] == "target" and bk.trades[-1]["exit"] == pytest.approx(110.0)
    assert bk.stats["target_hit"] == 1


def test_gap_through_target_fills_at_the_better_open():
    bk = long_book(rule=None)
    bk.on_bar(b(1, 112.0, 113.0, 111.0, 112.5))
    assert bk.trades[-1]["exit"] == pytest.approx(112.0)


def test_stop_wins_when_both_touched_in_one_bar():
    bk = long_book(rule=None)
    bk.on_bar(b(1, 100.0, 111.0, 94.0, 100.0))
    assert bk.trades[-1]["reason"] == "stop"


def test_short_target():
    bk = PaperBook(1000.0, ZERO, None)
    bk.submit(short_cmd(stop=105.0), T0)                       # fixture target is for a long: build a short one
    bk.on_bar(b(0, 100.0))
    pos = bk.positions["BTCUSDT"]
    pos.target = 90.0
    bk.on_bar(b(1, 95.0, 96.0, 89.0, 91.0))
    assert bk.trades[-1]["reason"] == "target" and bk.trades[-1]["exit"] == pytest.approx(90.0)


# ------------------------------------------------------------ L1: the owner's scenario

def path_to_near_target_then_back(bk):
    """+8% (80% of the way to a +10% target), then all the way down through the original stop."""
    px = [100, 102, 104, 106, 108, 106, 104, 102, 100, 98, 96, 94, 92]
    for i, p in enumerate(px[1:], start=1):
        bk.on_bar(b(i, px[i - 1], max(px[i - 1], p), min(px[i - 1], p), p))
    return bk


def test_without_ratchet_the_near_target_trade_becomes_a_full_loss():
    bk = path_to_near_target_then_back(long_book(rule=None))
    t = bk.trades[-1]
    assert t["reason"] == "stop" and t["pnl"] < 0
    assert t["mfe"] == pytest.approx(0.08)


def test_with_ratchet_the_same_trade_keeps_a_profit():
    bk = path_to_near_target_then_back(long_book())
    t = bk.trades[-1]
    assert t["reason"] == "stop" and t["pnl"] > 0
    assert t["exit"] == pytest.approx(104.0)                   # entry + 50% of the 8% excursion
    assert bk.stats["stop_tightened:ratchet"] >= 1
    assert all(a.source == "ratchet" for a in bk.adjustments)
    stops = [a.new_stop for a in bk.adjustments]
    assert stops == sorted(stops)                               # only ever up


def test_ratchet_stop_applies_from_the_next_bar_not_the_bar_it_was_computed_on():
    bk = long_book()
    bk.on_bar(b(1, 100.0, 108.0, 103.5, 107.0))                # arms at this close: stop -> 104
    assert "BTCUSDT" in bk.positions                            # the low 103.5 was BEFORE the stop moved
    assert bk.positions["BTCUSDT"].stop == pytest.approx(104.0)


def test_ratchet_exit_at_next_open_when_lock_is_through_the_close():
    bk = long_book()
    bk.on_bar(b(1, 100.0, 108.0, 100.0, 103.0))                # lock 104 > close 103: exit, not an instant stop
    assert bk.stats["ratchet_exit_queued"] == 1
    bk.on_bar(b(2, 103.2, 103.5, 102.0, 103.0))
    t = bk.trades[-1]
    assert t["reason"] == "ratchet_exit" and t["exit"] == pytest.approx(103.2)


def test_amend_stop_refuses_to_loosen_or_cross():
    bk = long_book(rule=None)
    assert bk.amend_stop("BTCUSDT", 90.0, T0 + M, "jev", "loosen") is None
    assert bk.amend_stop("BTCUSDT", 100.5, T0 + M, "jev", "through the last price") is None
    assert bk.positions["BTCUSDT"].stop == 95.0
    adj = bk.amend_stop("BTCUSDT", 97.0, T0 + M, "jev", "tighten")
    assert adj is not None and bk.positions["BTCUSDT"].stop == 97.0
    assert bk.stats["amend_refused:not_tighter"] == 2
    assert bk.amend_stop("ETHUSDT", 1.0, T0, "jev", "none") is None


def test_queued_exit_survives_kill_switch_cancel():
    bk = long_book(rule=None)
    assert bk.queue_exit("BTCUSDT", "advice_exit")
    bk.cancel_pending(T0 + M, "kill_switch")                   # cancels ORDERS; a risk-reducing exit stays
    bk.on_bar(b(1, 101.0))
    assert bk.trades[-1]["reason"] == "advice_exit"


def test_mfe_mae_tracked():
    bk = long_book(rule=None)
    bk.on_bar(b(1, 100.0, 103.0, 98.0, 101.0))
    p = bk.positions["BTCUSDT"]
    assert p.mfe() == pytest.approx(0.03) and p.mae() == pytest.approx(-0.02)
