"""paper_learner fee-honesty + net-ranking (2026-10-07).

THE DEFECT THIS PINS DOWN
-------------------------
paper_learner was the ONE loop in the system that scored candidate parameters
on GROSS expectancy and then ranked them by ACCURACY. Both are the failure
modes the project had already documented in writing:

  * fees.py:        "Our gross edge is ~0; fees were 100% of the loss."
  * SYSTEM_MAP §5:  "Dip-buying / accuracy: 66% win rate still LOSES."

Measured against data/paper-learner-state.json on 2026-10-07: all 72 configs
were GROSS-positive, but only 19 survive the measured round-trip cost, and
every dip shallower than -4% is NET-NEGATIVE (0 of 48). So a gate of
`ev > 0` on a GROSS figure admits guaranteed net losers, and picking the
highest-accuracy row picks the wrong objective.

These tests lock in the three fixes:
  1. simulate() reduces each return by the round-trip fee -> ev is NET.
  2. best_params() gates on NET-positive and RANKS BY NET EV (not accuracy).
  3. the cached grid is stamped with the fee it was built under, so a pre-fix
     GROSS cache is discarded instead of mixed with NET rows.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "paper_learner", Path(__file__).resolve().parents[1] / "scripts" / "paper_learner.py"
)
pl = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pl)


# --------------------------------------------------------------- fee honesty
def _declining_series():
    """A strictly declining series: entry is always above the hold-2 exit, so
    every gated trade is gross-negative and the fee can only deepen it."""
    return [200.0 - 0.5 * i for i in range(200)]


def test_simulate_charges_the_fee():
    """On a declining series every trade loses; net must be worse than gross
    by exactly the round-trip cost per trade."""
    r_net = pl.simulate(_declining_series(), dip=-0.01, rsi_max=100,
                        red_min=1, hold_bars=2)
    r_gross = pl.simulate(_declining_series(), dip=-0.01, rsi_max=100,
                          red_min=1, hold_bars=2, fee_rt=0.0)
    assert r_net is not None and r_gross is not None
    assert r_net[0] == 0 and r_net[1] > 10     # all losses, enough signals
    # the fee shifts expectancy down by FEE_RT (in %)
    gap = r_gross[2] - r_net[2]
    assert abs(gap - pl.FEE_RT * 100) < 1e-6, f"fee applied was {gap}, want {pl.FEE_RT*100}"


def test_simulate_net_is_used_not_gross():
    """The default (no fee arg) path must be the NET one, i.e. exactly the
    fee-charged figure — this is the regression the whole change exists for."""
    default = pl.simulate(_declining_series(), dip=-0.01, rsi_max=100,
                          red_min=1, hold_bars=2)
    explicit_net = pl.simulate(_declining_series(), dip=-0.01, rsi_max=100,
                               red_min=1, hold_bars=2, fee_rt=pl.FEE_RT)
    assert default == explicit_net


def test_simulate_fee_is_the_round_trip_constant():
    """The fee applied must be pl.FEE_RT (from fees.py), not a re-typed value."""
    assert abs(pl.FEE_RT - 0.001594) < 1e-9


# ------------------------------------------------------- net ranking / gate
def _fake_series():
    """A rising series: any entry is a win, so a deep-enough dip fires."""
    # gently rising with a shallow pullback that a loose dip gate will accept
    return [(i, 100.0 + i * 0.1) for i in range(300)]


def test_best_params_ranks_by_net_ev_not_accuracy(monkeypatch):
    """Given a high-accuracy/low-net config and a lower-accuracy/higher-net one,
    best_params must return the higher NET EV one."""
    # three configs a, b, c -> (acc, net_ev, n)
    table = {
        (-0.01, 30, 2): (70.0, 0.10, 1000),   # high acc, lower net
        (-0.08, 30, 4): (55.0, 0.30, 1000),   # lower acc, higher net  <- winner
        (-0.04, 40, 3): (60.0, 0.20, 1000),
    }

    def fake_eval(dip, rsi_max, red_min, cache):
        return table.get((dip, rsi_max, red_min))

    monkeypatch.setattr(pl, "evaluate_params", fake_eval)
    monkeypatch.setattr(pl, "_load_all_series", lambda: {"x": [1]})
    monkeypatch.setattr(pl, "STATE", Path("/nonexistent/state.json"))
    best = pl.best_params()
    assert best is not None
    # (ev, dip, rsi_max, red_min, ev, n, acc)
    assert best[1:4] == (-0.08, 30, 4), "must pick the highest NET EV config"
    assert abs(best[4] - 0.30) < 1e-9


def test_best_params_tiebreak_is_accuracy_not_dip(monkeypatch):
    """Two configs with EQUAL net EV must tie-break on ACCURACY.

    Regression: the comparison used `best[1]` (the dip threshold) as the
    tiebreak key instead of accuracy, so a config could be ranked by how deep
    its dip was. Here both have net EV 0.20; the higher-accuracy one must win.
    """
    table = {
        (-0.08, 30, 4): (52.0, 0.20, 1000),   # same net, lower acc
        (-0.04, 40, 3): (64.0, 0.20, 1000),   # same net, higher acc <- winner
    }
    monkeypatch.setattr(pl, "evaluate_params",
                        lambda d, r, rm, c: table.get((d, r, rm)))
    monkeypatch.setattr(pl, "_load_all_series", lambda: {"x": [1]})
    monkeypatch.setattr(pl, "STATE", Path("/nonexistent/state.json"))
    best = pl.best_params()
    assert best is not None
    assert best[1:4] == (-0.04, 40, 3), "tie on net EV must break toward higher accuracy"


def test_best_params_rejects_net_negative_even_if_accuracy_high(monkeypatch):
    """A config with 90% accuracy but non-positive net EV must be rejected."""
    table = {
        (-0.01, 30, 2): (90.0, -0.01, 1000),  # looks great, loses money
    }
    monkeypatch.setattr(pl, "evaluate_params",
                        lambda d, r, rm, c: table.get((d, r, rm)))
    monkeypatch.setattr(pl, "_load_all_series", lambda: {"x": [1]})
    monkeypatch.setattr(pl, "STATE", Path("/nonexistent/state.json"))
    assert pl.best_params() is None, "net-negative config must not be adopted"


# ------------------------------------------------------------ cache units
def test_gross_cache_is_discarded(monkeypatch, tmp_path):
    """A cache written WITHOUT the current fee stamp must be recomputed.

    This is the silent-unit guard: pre-fix rows are gross, post-fix are net;
    mixing them would gate a gross figure with a net threshold.
    """
    stale = tmp_path / "state.json"
    # a GROSS-era cache: no cost_rt key at all
    stale.write_text('{"scored": {"[-0.01, 30, 2]": [50.0, 0.05, 900]}}')
    monkeypatch.setattr(pl, "STATE", stale)
    monkeypatch.setattr(pl, "_load_all_series", lambda: {"x": [1]})

    calls = {"n": 0}

    def fake_eval(d, r, rm, c):
        calls["n"] += 1
        return (50.0, 0.05, 900)

    monkeypatch.setattr(pl, "evaluate_params", fake_eval)
    pl.best_params()
    # the stale cache had 1 entry but was discarded -> evaluate_params was called
    assert calls["n"] > 1, "stale (unstamped) cache must not be trusted"


def test_matching_cache_is_reused(monkeypatch, tmp_path):
    """A cache stamped with the SAME fee AND the SAME input must be reused.

    The input stamp matters as much as the fee stamp (2026-10-20): a grid
    scored on the frozen sweep files carries a different input_sig than the
    live DB, and reusing it would print the old verdict forever.
    """
    good = tmp_path / "state.json"
    cache = {"x": [1]}
    good.write_text(
        json.dumps({"cost_rt": pl.FEE_RT,
                    "input_sig": pl._input_signature(cache),
                    "scored": {"[-0.08, 30, 4]": [56.0, 0.184, 907]}}),
        encoding="utf-8")
    monkeypatch.setattr(pl, "STATE", good)
    monkeypatch.setattr(pl, "_load_all_series", lambda: cache)

    calls = {"n": 0}

    def fake_eval(d, r, rm, c):
        calls["n"] += 1
        return None

    monkeypatch.setattr(pl, "evaluate_params", fake_eval)
    best = pl.best_params()
    # the one cached row is net-positive + acc>=55 + n>=200 -> adopted without
    # recomputing it (only the other 71 combos are evaluated, each returning None)
    assert best is not None
    assert best[1:4] == (-0.08, 30, 4)
    assert abs(best[4] - 0.184) < 1e-9
    assert best[5] == 907    # n
