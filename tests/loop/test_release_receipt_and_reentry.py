"""The 2026-11-01 E9 repair wrote a receipt that no code can ever write.

Found on 2026-11-02 while verifying E9 on the first H4 bar after the fix. The
repair path (`released_phantom_claim`) sets `in_pos/symbol/qty/entry/ledger_id`
and nothing else. The LIVE state file, however, carries a field on the released
arm that appears nowhere in `scripts/`:

    "released_unowned_position": {
        "entry": 2241.36, "symbol": "ETHUSDT",
        "why": "phantom adoption by a --dry-run pass that persisted state ...
                released again 2026-11-01 and the PAIRING EXEMPTION that let it
                re-adopt is itself fixed in 4xx (E9). Owner is ..."}

Three problems with that bookkeeping, in order of how much they will hurt:

1. NO CODE WRITES IT, so no test can read it and no reviewer can trust it. The
   released arm's decision-log line for 2026-11-01 is a plain `hold` at 16:07
   (the PRE-fix code still claiming), then `symbol_owned_by_other_strategy` at
   20:07 -- there is no `released_phantom_claim` row anywhere in the log. A
   repair that exists only in the state file is a repair with no receipt.
   (The 10-31 pass had already written one for this same claim, and the
   11-01 JOURNAL's "post-fix replay ... `released_phantom_claim`" refers to a
   replay against a temp state, not to the live pass.)

2. IT CONTRADICTS THE LOG IT DESCRIBES. "fixed in 4xx" names a commit that does
   not exist (`git log --all`), and `entry 2241.36` is the 10-31 snapshot of a
   position price, not the release price or the release bar. The 11-01 release
   itself happened at 20:07, on the 20:00 bar, close 2258.42.

3. IT IS UNBOUNDED. `strat_state()` never prunes it and the release path never
   refreshes it, so the next release of the same arm -- or a buy of a different
   symbol months later -- still carries "released again 2026-11-01". A stale
   receipt is worse than no receipt: it is the field a human will read when they
   next have to decide whether the arm can be trusted.

The invariant this file pins: every release the state can show must be dated and
attributed by the pass that made it, and nothing else may write that record.
"""
from __future__ import annotations

import copy
import importlib.util
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


def _mod():
    path = REPO / "scripts" / "run_crypto_trend_demo.py"
    spec = importlib.util.spec_from_file_location("_demo_runner_e10", path)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _load() -> tuple[dict, dict]:
    return json.loads(STATE.read_text()), json.loads(REGISTRY.read_text())


def _pair(reg: dict):
    by_id = {s["id"]: s for s in reg["strategies"]}
    for s in reg["strategies"]:
        if s.get("match"):
            return by_id[s["match"]]["id"], s["id"]
    pytest.skip("registry declares no matched control pair")


def _run_once_on(tmp_path, monkeypatch, state: dict, reg: dict, *,
                 dry: bool = False, close1: float = 2258.42) -> dict:
    """Run the SHIPPED run_once against a temp copy. No orders, no network."""
    m = _mod()
    st_path = tmp_path / "state.json"
    st_path.write_text(json.dumps(state))
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
    monkeypatch.setattr(m, "position", lambda sym: (
        {"side": "Buy", "avgPrice": 2241.49, "size": 1.2}
        if sym == "ETHUSDT" else None))
    bar = 1793577600000          # 2026-11-02T00:00Z, a real closed H4 bar
    monkeypatch.setattr(m, "klines", lambda sym, iv, limit=400: (
        [bar] * 400, [0.0] * 400, [close1 + 2] * 400, [close1 - 2] * 400,
        [close1] * 400))
    monkeypatch.setattr(m, "place_market", lambda *a, **k: pytest.fail(
        "run_once placed a real order during a test"))
    monkeypatch.setattr(m, "instrument", lambda symbol: {
        "tick": 0.01, "step": 0.1, "min_qty": 0.1, "min_notional": 0.0})
    return m.run_once(dry=dry)


def _decisions(tmp_path) -> list[dict]:
    p = tmp_path / "decisions.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _phantom_setup(state: dict, reg: dict):
    treat, ctrl = _pair(reg)
    state = copy.deepcopy(state)
    state["strategies"][treat].update(
        {"in_pos": True, "symbol": "ETHUSDT", "qty": 1.2, "entry": 2241.49,
         "stop": 2192.37, "ledger_id": None})
    state["strategies"][treat].pop("owns_exchange_position", None)
    state["strategies"][ctrl].update(
        {"in_pos": True, "symbol": "ETHUSDT", "qty": 1.2, "entry": 2241.49,
         "ledger_id": "ea-RANDOM-1", "owns_exchange_position": True})
    for s in state["strategies"].values():
        s["last_bar"] = 0
    return state, treat, ctrl


# ------------------------------------------------------------------ 1. receipt
def test_a_release_is_written_to_the_decision_log_by_the_pass_that_did_it(
        tmp_path, monkeypatch):
    """STATE VALIDITY, not 'a temp file looks different'.

    The live log has no `released_phantom_claim` row for the 11-01 release even
    though the state says it happened: the live release was done by a script, not
    by the shipped pass. On a pass that can write state, the log must carry it.
    """
    state, reg = _load()
    state, treat, ctrl = _phantom_setup(state, reg)
    before = len(_decisions(tmp_path))
    out = _run_once_on(tmp_path, monkeypatch, state, reg)
    rows = _decisions(tmp_path)
    new = rows[before:]
    releases = [r for r in new if r.get("event") == "released_phantom_claim"]
    assert len(releases) == 1, (
        f"the pass released the claim but logged {len(releases)} release "
        f"rows; events were {[r.get('event') for r in new]}")
    rec = releases[0]
    assert rec["id"] == treat
    assert rec["owner"] == ctrl, "a release must name who could prove ownership"
    assert rec.get("bar"), "a release must be dated by the bar it ran on"
    assert treat in out.get("strategies", {}), "released arm was not evaluated"


def test_a_dry_pass_announces_the_release_it_would_make(tmp_path, monkeypatch):
    """A rehearsal must not write state -- but silence is not a rehearsal.

    `run_once(dry=True)` saves nothing, so the only evidence it would release a
    phantom is the decision row. Without it the operator rehearses, sees a clean
    report, and learns nothing about the repair.
    """
    state, reg = _load()
    state, treat, ctrl = _phantom_setup(state, reg)
    before = len(_decisions(tmp_path))
    _run_once_on(tmp_path, monkeypatch, state, reg, dry=True)
    new = _decisions(tmp_path)[before:]
    releases = [r for r in new if r.get("event") == "would_release_phantom_claim"]
    assert len(releases) == 1, (
        f"dry pass released nothing on paper; events "
        f"{[r.get('event') for r in new]}")
    assert releases[0]["id"] == treat and releases[0]["owner"] == ctrl
    assert releases[0].get("dry") is True
    # And it really did not persist: rehearsing leaves no trace.
    on_disk = json.loads((tmp_path / "state.json").read_text())["strategies"]
    assert on_disk[treat]["in_pos"] is True


# ------------------------------------------------------- 2. the state receipt
def test_a_released_arm_carries_no_receipt_field(tmp_path, monkeypatch):
    """A receipt no code writes cannot be audited, so it must not be there.

    This is the live defect: `released_unowned_position` exists in state.json,
    references a commit that does not exist ("4xx"), and is never refreshed or
    pruned. The decision log is the record.
    """
    state, reg = _load()
    state, treat, ctrl = _phantom_setup(state, reg)
    state["strategies"][treat]["released_unowned_position"] = {
        "entry": 2241.36, "symbol": "ETHUSDT", "why": "stale receipt"}
    _run_once_on(tmp_path, monkeypatch, state, reg)
    after = json.loads((tmp_path / "state.json").read_text())["strategies"]
    assert after[treat]["in_pos"] is False
    assert "released_unowned_position" not in after[treat], (
        "the released arm still carries an unrefreshed receipt; the pass must "
        "clear it so state and decision log cannot disagree")


# ------------------------------------------- 3. what happens NEXT is unpinned
def test_a_released_arm_can_buy_its_symbol_after_a_real_exit(
        tmp_path, monkeypatch):
    """The invariant E9 actually needs: release does not poison the arm.

    After the owner closes and the exchange is flat, the treatment must be able
    to take its OWN entry -- the turtle engine fires a buy, and the purchase is
    recorded with this arm's own ledger id.
    """
    state, reg = _load()
    state, treat, ctrl = _phantom_setup(state, reg)
    m = _mod()
    # ticker flat after the control exits: nobody holds ETH any more
    monkeypatch.setattr(m, "position", lambda sym: None)
    state["strategies"][ctrl].update({"in_pos": False, "symbol": None,
                                      "qty": 0.0, "ledger_id": None})
    # turtle wants = close > max(high[-56:-1]); a flat + rising series never
    # fires, a jump on the last bar does. Build it so the entry rule fires.
    bar = 1793577600000
    ts = [bar - (399 - i) * 14_400_000 for i in range(400)]
    highs = [2260.0] * 399 + [2280.0]
    closes = [2255.0] * 399 + [2285.0]
    lows = [2240.0] * 399 + [2270.0]
    monkeypatch.setattr(m, "klines", lambda sym, iv, limit=400: (
        ts, [0.0] * 400, highs, lows, closes))
    monkeypatch.setattr(m, "place_market", lambda *a, **k: {
        "retCode": 0, "retMsg": "OK", "result": {"orderId": "test-1"}})
    st_path = tmp_path / "state.json"
    st_path.write_text(json.dumps(state))
    eth = {"strategies": [s for s in reg["strategies"]
                          if s.get("symbol") == "ETHUSDT"]}
    reg_path = tmp_path / "strategies.json"
    reg_path.write_text(json.dumps(eth))
    monkeypatch.setattr(m, "RUNNER_DIR", tmp_path)
    monkeypatch.setattr(m, "STATE", st_path)
    monkeypatch.setattr(m, "REGISTRY", reg_path)
    monkeypatch.setattr(m, "DECISIONS", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(m, "PUBLISH", False)
    monkeypatch.setattr(m, "instrument", lambda symbol: {
        "tick": 0.01, "step": 0.1, "min_qty": 0.1, "min_notional": 0.0})
    _run_once_on  # keep the helper referenced; this test needs custom klines
    m.PUBLISH = True   # this test is about the committed id, not the chain
    m.run_once(dry=False)
    after = json.loads(st_path.read_text())["strategies"][treat]
    assert after["in_pos"] is True, (
        "a released arm could not take a legitimate entry on a symbol nobody "
        "owns -- the release poisoned the arm")
    assert after.get("ledger_id"), (
        "the arm's own entry must carry a committed ledger id, or the next "
        "pass releases it as a phantom")
    assert after["symbol"] == "ETHUSDT"
