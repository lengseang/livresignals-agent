"""The ownership guard must not disarm a strategy's own decisions.

Regression for the 2026-10-31 follow-up to `294d5ae` ("two strategies claiming
one exchange position"). That fix stopped a strategy adopting a position another
strategy already owned, which was right. But it placed the guard *before*
`decide()` and made it fire whenever ANY other strategy held the symbol, which
broke two things that the live decision log proves:

1. `RANDOM_ENTRY_CONTROL` is the luck baseline for `ETH_TURTLE_H4` (registry
   field `match`), same symbol, same exits. When the control happens to hold the
   ETH position, the guard skips the TREATMENT. That is the one pair that must
   never be mutually exclusive: suppressing the treatment while its control
   trades destroys exactly the comparison the runner exists to make.

2. The guard `continue`s before `decide()`, so a skipped strategy never
   evaluates its own exit, never advances `last_bar`, and lags the book. In the
   live state ETH_TURTLE_H4 latched at bar 04:00 UTC while the control reached
   12:00 UTC -- three H4 bars behind.

3. An owned position's exit must never be blocked by the guard. If a strategy
   legitimately holds a position it must still be able to close it even if
   another strategy is mid-adoption on the same symbol.

The rule: ownership decides ADOPTION, never DECISION. And a paired treatment /
control may never be treated as rival claimants, because they are not two
strategies contesting one position -- they are two arms of one experiment.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path("/opt/trading-agent")
STATE = REPO / "state" / "crypto-trend-demo" / "state.json"
REGISTRY = REPO / "state" / "crypto-trend-demo" / "strategies.json"

pytestmark = pytest.mark.skipif(
    not (STATE.exists() and REGISTRY.exists()),
    reason="live demo runner state/registry not present on this machine",
)


def _load():
    return json.loads(STATE.read_text()), json.loads(REGISTRY.read_text())


def _pair(reg: dict) -> tuple[dict, dict]:
    """The treatment and its matched control, from the registry itself."""
    by_id = {s["id"]: s for s in reg["strategies"]}
    for s in reg["strategies"]:
        if s.get("match"):
            return by_id[s["match"]], s
    pytest.skip("registry declares no matched control pair")


def _guard(strategies: dict, sid: str, sym: str, *, paired: set[str]) -> str | None:
    """Call the runner's SHIPPED ownership rule, not a copy of it.

    `run_crypto_trend_demo` is imported for its pure helpers only; module import
    must not place orders or touch the network (it does neither at import time).
    """
    import importlib.util

    path = REPO / "scripts" / "run_crypto_trend_demo.py"
    spec = importlib.util.spec_from_file_location("_demo_runner_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    st = {"strategies": strategies}
    return mod.symbol_owner(st, sid, sym, paired=paired)


def test_live_state_has_the_pair_on_one_symbol():
    """Guard the premise: if the pair is not co-located this test proves nothing."""
    st, reg = _load()
    treatment, control = _pair(reg)
    assert treatment["symbol"] == control["symbol"], (
        "pair no longer shares a symbol; the regression scenario does not apply"
    )
    assert control["id"] == "RANDOM_ENTRY_CONTROL"
    assert treatment["id"] == "ETH_TURTLE_H4"


def test_control_holding_the_symbol_must_not_disarm_its_treatment():
    """The live bug: the luck baseline held ETH, so the treatment went dark."""
    st, reg = _load()
    treatment, control = _pair(reg)
    strategies = st["strategies"]

    # Force the exact live condition rather than depend on it: control in pos.
    strategies[treatment["id"]].update({"in_pos": False, "symbol": None})
    strategies[control["id"]].update(
        {"in_pos": True, "symbol": treatment["symbol"]})

    paired = {treatment["id"], control["id"]}
    blocker = _guard(strategies, treatment["id"], treatment["symbol"],
                     paired=paired)
    assert blocker is None, (
        f"{treatment['id']} was blocked by its own control {blocker}; "
        f"a treatment must keep trading while its luck baseline holds the symbol"
    )


def test_paired_arms_are_not_rivals():
    """Symmetry: the control must be equally free to run."""
    st, reg = _load()
    treatment, control = _pair(reg)
    strategies = st["strategies"]
    strategies[treatment["id"]].update(
        {"in_pos": True, "symbol": treatment["symbol"]})
    strategies[control["id"]].update({"in_pos": False, "symbol": None})

    paired = {treatment["id"], control["id"]}
    blocker = _guard(strategies, control["id"], control["symbol"],
                     paired=paired)
    assert blocker is None, f"control was blocked by its treatment {blocker}"


def test_owned_position_exit_is_never_blocked():
    """A strategy that legitimately holds a position must always reach decide().

    Note the guard's contract: run_once() only *consults* it on the adoption
    branch (exch_in and not local_in), so an owner is never skipped anyway.
    This asserts the helper agrees -- an owner is not "blocked" by a stranger
    mid-adoption -- so the rule cannot be re-wired into the decision path later.
    """
    st, reg = _load()
    treatment, _control = _pair(reg)
    strategies = dict(st["strategies"])
    strategies["SOME_OTHER"] = {"in_pos": True, "symbol": treatment["symbol"]}
    strategies[treatment["id"]] = {
        **(strategies.get(treatment["id"]) or {}),
        "in_pos": True, "symbol": treatment["symbol"],
    }
    # paired = treatment + its registry partner; SOME_OTHER is a true stranger.
    paired = {treatment["id"], _control["id"]}
    blocker = _guard(strategies, treatment["id"], treatment["symbol"],
                     paired=paired)
    assert blocker is None, "an owned position's exit was blocked by the guard"


def test_unpaired_stranger_still_blocked_from_adopting():
    """The original 294d5ae fix must survive this change."""
    st, reg = _load()
    treatment, _control = _pair(reg)
    strategies = {
        "OWNER": {"in_pos": True, "symbol": treatment["symbol"]},
        "STRANGER": {"in_pos": False, "symbol": None},
    }
    blocker = _guard(strategies, "STRANGER", treatment["symbol"], paired=set())
    assert blocker == "OWNER", "a stranger was allowed to double-claim a symbol"


def test_treatment_does_not_lag_the_book():
    """Live evidence of the symptom: last_bar desync from a skipped strategy.

    This asserts the *consequence* the user can see, not the mechanism, so it
    fails loudly if the guard regresses in a new shape.
    """
    st, reg = _load()
    treatment, control = _pair(reg)
    strategies = st["strategies"]
    t, c = strategies.get(treatment["id"], {}), strategies.get(control["id"], {})
    if not t.get("last_bar") or not c.get("last_bar"):
        pytest.skip("runner has not polled yet")
    lag_bars = (c["last_bar"] - t["last_bar"]) / (240 * 60 * 1000)
    assert lag_bars <= 1, (
        f"{treatment['id']} is {lag_bars:.0f} closed H4 bars behind its control; "
        f"a strategy skipped by the ownership guard never advances last_bar"
    )
