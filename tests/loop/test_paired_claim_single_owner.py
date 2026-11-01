"""A paired treatment/control are two arms of one experiment, NOT two owners.

Regression for E9 (2026-11-01). The 92db5bb fix exempted paired arms from the
ownership guard, which was right about DECISION and wrong about CLAIM: the flat
treatment then adopted the control's position (writing `ledger_id: None`) and
both arms held `in_pos:true, qty 1.2` on the exchange's single ETH position for
two days. `test_live_state_has_no_unowned_double_claim` was red on it.

The invariant this file pins:

  * two arms may both be ENABLED on one symbol (no rivalry, no lag), but
  * only ONE arm may CLAIM the exchange's single position, and
  * an arm with no committed ledger id never claims a symbol another arm owns.

These tests call the SHIPPED `run_once()` on a temp state — the 2026-10-19
NameError shipped because the suite tested helpers and never called main().
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path("/opt/trading-agent")
STATE = REPO / "state" / "crypto-trend-demo" / "state.json"
REGISTRY = REPO / "state" / "crypto-trend-demo" / "strategies.json"

pytestmark = pytest.mark.skipif(
    not (STATE.exists() and REGISTRY.exists()),
    reason="live demo runner state/registry not present on this machine",
)


def _mod():
    path = REPO / "scripts" / "run_crypto_trend_demo.py"
    spec = importlib.util.spec_from_file_location("_demo_runner_e9", path)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _load():
    return json.loads(STATE.read_text()), json.loads(REGISTRY.read_text())


def _pair(reg: dict):
    by_id = {s["id"]: s for s in reg["strategies"]}
    for s in reg["strategies"]:
        if s.get("match"):
            return by_id[s["match"]]["id"], s["id"]
    pytest.skip("registry declares no matched control pair")


# ---------------------------------------------------------------- helpers
def test_pair_member_holding_the_symbol_blocks_a_second_claim():
    """The live defect in one assertion: partner holds it -> do NOT re-claim."""
    m = _mod()
    treat, ctrl = _pair(_load()[1])
    strategies = {
        treat: {"in_pos": False, "symbol": None, "ledger_id": None},
        ctrl: {"in_pos": True, "symbol": "ETHUSDT", "qty": 1.2,
               "ledger_id": "ea-X", "owns_exchange_position": True},
    }
    paired = {treat, ctrl}
    blocker = m.symbol_owner({"strategies": strategies}, treat, "ETHUSDT",
                             paired=paired)
    assert blocker == ctrl, (
        "a paired arm was allowed to claim the same physical position its "
        "partner owns -- this is the E9 double-claim"
    )


def test_pair_member_is_still_free_to_decide_flat():
    """No rivalry: when NEITHER arm holds the symbol, neither is blocked.

    The distinction the fix must preserve: blocking a CLAIM is not blocking an
    ARM. decide() always runs; only adoption is refused.
    """
    m = _mod()
    treat, ctrl = _pair(_load()[1])
    strategies = {
        treat: {"in_pos": False, "symbol": None},
        ctrl: {"in_pos": False, "symbol": None},
    }
    paired = {treat, ctrl}
    for sid in (treat, ctrl):
        assert m.symbol_owner({"strategies": strategies}, sid, "ETHUSDT",
                              paired=paired) is None


def test_position_holder_prefers_the_committed_owner():
    """In a corrupt state, the proof of ownership decides who the owner is."""
    m = _mod()
    treat, ctrl = _pair(_load()[1])
    st = {"strategies": {
        treat: {"in_pos": True, "symbol": "ETHUSDT", "qty": 1.2, "ledger_id": None},
        ctrl: {"in_pos": True, "symbol": "ETHUSDT", "qty": 1.2,
               "ledger_id": "ea-X", "owns_exchange_position": True},
    }}
    holder = m.position_holder(st, "ETHUSDT")
    assert holder is not None
    sid, disputed = holder
    assert sid == ctrl, f"picked the ledger_id:None phantom as owner ({sid})"
    assert disputed is True, "two claims on one symbol must read as disputed"


def test_position_holder_is_quiet_when_uncontested():
    m = _mod()
    treat, ctrl = _pair(_load()[1])
    st = {"strategies": {
        treat: {"in_pos": True, "symbol": "ETHUSDT", "ledger_id": "ea-X"},
        ctrl: {"in_pos": False, "symbol": None},
    }}
    assert m.position_holder(st, "ETHUSDT") == (treat, False)


def test_run_once_repairs_a_phantom_claim_on_a_NO_NEW_BAR_pass(
        tmp_path, monkeypatch):
    """The gap the live replay caught: `no_new_bar` returns before every guard.

    A corrupt state survived two days because the treatment had already seen
    the H4 bar, so the pass hit `if s["last_bar"] == ts[-1]: continue` and
    returned before the ownership logic. Repair is about state validity, so it
    must run ahead of the bar short-circuit.
    """
    state, reg = _load()
    treat, ctrl = _pair(reg)
    state = copy.deepcopy(state)
    state["strategies"][treat].update(
        {"in_pos": True, "symbol": "ETHUSDT", "qty": 1.2, "entry": 2241.49,
         "ledger_id": None})
    state["strategies"][ctrl].update(
        {"in_pos": True, "symbol": "ETHUSDT", "qty": 1.2, "entry": 2241.49,
         "ledger_id": "ea-RANDOM-1", "owns_exchange_position": True})
    BAR = 1793548800000
    for s in state["strategies"].values():
        s["last_bar"] = BAR          # <- the short-circuit condition

    out = _run_once_on(tmp_path, monkeypatch, state, reg, dry=False)

    after = json.loads((tmp_path / "state.json").read_text())["strategies"]
    assert after[treat]["in_pos"] is False, (
        "phantom claim survived a no-new-bar pass: the repair is behind the "
        "`continue` and will never run for an arm that already saw the bar"
    )
    assert after[ctrl]["in_pos"] is True
    assert out["strategies"][treat].get("no_new_bar") is True


# ------------------------------------------------------- end-to-end on run_once
def _run_once_on(tmp_path, monkeypatch, state: dict, reg: dict, dry: bool = True) -> dict:
    """Run the SHIPPED run_once against a temp copy. No orders, no network.

    ⚠️ `RUNNER_DIR` is patched as well as `STATE`/`REGISTRY`: `log_decision()`
    writes to `RUNNER_DIR/decisions.jsonl`, so patching only `STATE` leaves the
    test appending fixture rows to the LIVE decision log. Same trap as
    `f"file:{Path}?mode=ro"` opening the live store.
    """
    m = _mod()
    st_path = tmp_path / "state.json"
    st_path.write_text(json.dumps(state))
    # Scope the pass to the ETH pair: a fixture-driven candle series makes the
    # OTHER arms (BTC_TURTLE_H1 etc.) want to buy too, which is unrelated noise
    # for this test and tripped the place_market guard.
    eth = {"strategies": [s for s in reg["strategies"]
                          if s.get("symbol") == "ETHUSDT"]}
    assert len(eth["strategies"]) >= 2, "fixture needs the ETH pair"
    reg_path = tmp_path / "strategies.json"
    reg_path.write_text(json.dumps(eth))
    monkeypatch.setattr(m, "RUNNER_DIR", tmp_path)
    monkeypatch.setattr(m, "STATE", st_path)
    monkeypatch.setattr(m, "REGISTRY", reg_path)
    monkeypatch.setattr(m, "DECISIONS", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(m, "PUBLISH", False)
    # No exchange access: report the symbol as flat-or-held from the fixture.
    monkeypatch.setattr(m, "position", lambda sym: (
        {"side": "Buy", "avgPrice": 2241.49, "size": 1.2}
        if sym == "ETHUSDT" else None))
    # A real bar timestamp, NOT zeros: run_once() short-circuits on
    # `s["last_bar"] == ts[-1]`, and the fixture above seeds last_bar=0. A
    # zero-filled time axis makes the pass a no-op and every assertion vacuous.
    bar = 1793548800000
    monkeypatch.setattr(m, "klines", lambda sym, iv, limit=400: (
        [bar] * 400, [0.0] * 400, [2260.0] * 400, [2240.0] * 400,
        [2266.19] * 400))
    monkeypatch.setattr(m, "place_market", lambda *a, **k: pytest.fail(
        "run_once placed a real order during a test"))
    # qty_for() prices the order via instrument(); stub it or the test reaches
    # the live Bybit endpoint (it did, and failed with KeyError 'result').
    monkeypatch.setattr(m, "instrument", lambda symbol: {
        "tick": 0.01, "step": 0.1, "min_qty": 0.1, "min_notional": 0.0})
    return m.run_once(dry=dry)


def test_run_once_releases_a_phantom_claim_on_a_partner_held_symbol(
        tmp_path, monkeypatch):
    """THE E9 CASE, end to end: the flat arm must not keep the claim."""
    state, reg = _load()
    treat, ctrl = _pair(reg)
    state = copy.deepcopy(state)
    state["strategies"][treat].update(
        {"in_pos": False, "symbol": None, "qty": 0.0, "entry": 0.0,
         "ledger_id": None})
    state["strategies"][ctrl].update(
        {"in_pos": True, "symbol": "ETHUSDT", "qty": 1.2, "entry": 2241.49,
         "ledger_id": "ea-RANDOM-1", "owns_exchange_position": True})
    # Force the pass to actually evaluate: stale last_bar everywhere.
    for s in state["strategies"].values():
        s["last_bar"] = 0

    # dry=False so the pass persists (a dry run deliberately writes nothing);
    # the exchange and order path are stubbed above, so nothing can trade.
    out = _run_once_on(tmp_path, monkeypatch, state, reg, dry=False)

    after = json.loads((tmp_path / "state.json").read_text())["strategies"]
    assert after[treat]["in_pos"] is False, (
        f"{treat} re-claimed ETHUSDT while {ctrl} owns it -- back to the "
        f"double claim on one exchange position"
    )
    assert after[treat].get("symbol") is None
    assert after[ctrl]["in_pos"] is True, "the real owner lost its position"
    # The treatment must still have been EVALUATED (no silent lag).
    assert treat in out.get("strategies", {}), (
        "the paired arm was skipped before decide() -- the 2026-10-31 lag bug"
    )
    assert after[treat]["last_bar"] != 0, (
        "a blocked arm did not advance its clock; it will lag the book"
    )


def test_run_once_does_not_exit_a_position_it_does_not_own(
        tmp_path, monkeypatch):
    """A block must never turn into a Sell of the partner's real position."""
    state, reg = _load()
    treat, ctrl = _pair(reg)
    state = copy.deepcopy(state)
    state["strategies"][treat].update(
        {"in_pos": True, "symbol": "ETHUSDT", "qty": 1.2, "entry": 2241.49,
         "ledger_id": None})
    state["strategies"][ctrl].update(
        {"in_pos": True, "symbol": "ETHUSDT", "qty": 1.2, "entry": 2241.49,
         "ledger_id": "ea-RANDOM-1", "owns_exchange_position": True})
    for s in state["strategies"].values():
        s["last_bar"] = 0

    # place_market fails the test if called; the close path is guarded anyway.
    _run_once_on(tmp_path, monkeypatch, state, reg, dry=False)
    after = json.loads((tmp_path / "state.json").read_text())["strategies"]
    assert after[ctrl]["in_pos"] is True
    assert after[ctrl]["qty"] == 1.2
