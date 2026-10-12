"""Regression test: a self-improve EDGE disable must actually stop native paper
openers from opening.

Defect found 2026-10-12: the self-improve engine writes proven-losing edges to
data/signals-state.json['disabled'] and `_imported_strategy_open` honoured it,
but the three NATIVE openers (`validated`, `loose`, `pullback`) never read the
list. When 'loose' was disabled on 2026-10-11 02:40, 44 loose trades still
closed afterward and 4 were open at audit time — the disable was COSMETIC.

These tests pin the single reader (`_edge_disabled`) and its wiring into each
native opener, so the disable can never again be decorative.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture()
def trader(tmp_path, monkeypatch):
    """Import unified_trader with DATA/state redirected to a temp dir.

    `unified_trader` is on tests/loop/conftest.py's BLOCKED_MODULES real-money
    list, and that guard fails any loop test if the module is already resident
    in sys.modules. So we import it scoped to this fixture and EVICT it on
    teardown — a stray top-level import here would poison every tests/loop/
    test that runs later in the same session.
    """
    sys.path.insert(0, str(SCRIPTS))
    was_present = "unified_trader" in sys.modules
    mod = importlib.import_module("unified_trader")
    try:
        data = tmp_path / "data"
        data.mkdir(exist_ok=True)
        paper = data / "unified-paper.json"
        monkeypatch.setattr(mod, "PAPER_STATE", paper, raising=False)
        monkeypatch.setattr(mod, "ROOT", tmp_path, raising=False)
        yield mod, data / "signals-state.json", paper
    finally:
        # Leave sys.modules exactly as we found it, so the tests/loop/ guard
        # (BLOCKED_MODULES ∩ sys.modules) stays clean.
        if not was_present:
            sys.modules.pop("unified_trader", None)


def _write_state(p: Path, disabled: list[str]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"disabled": disabled, "promoted": [],
                             "disabled_symbols": []}), encoding="utf-8")


def test_edge_disabled_reads_list(trader):
    mod, state, _ = trader
    _write_state(state, ["loose", "supertrend"])
    assert mod._edge_disabled("loose") is True
    assert mod._edge_disabled("supertrend") is True
    assert mod._edge_disabled("validated") is False
    assert mod._edge_disabled("") is False


def test_edge_disabled_missing_file_is_false(trader):
    mod, state, _ = trader
    if state.exists():
        state.unlink()
    # A missing/unreadable state file must NOT block trading.
    assert mod._edge_disabled("loose") is False


def test_loose_opener_respects_disable(trader):
    mod, state, paper = trader
    _write_state(state, ["loose"])
    sig = {"symbol": "AVAX/USDT", "tf": "5m", "close": 20.0, "dd": -0.02, "rsi": 40}
    assert mod._is_loose(sig) is True          # signal itself still qualifies
    assert mod._paper_loose_open(sig, "RANGING") is False
    assert not paper.exists() or not json.loads(paper.read_text())["open"]


def test_loose_opener_opens_when_not_disabled(trader):
    mod, state, paper = trader
    _write_state(state, [])                    # nothing disabled
    sig = {"symbol": "AVAX/USDT", "tf": "5m", "close": 20.0, "dd": -0.02, "rsi": 40}
    assert mod._paper_loose_open(sig, "RANGING") is True
    doc = json.loads(paper.read_text())
    assert len(doc["open"]) == 1
    assert doc["open"][0]["edge"] == "loose"


def test_validated_opener_respects_disable(trader):
    mod, state, paper = trader
    _write_state(state, ["validated"])
    sig = {"symbol": "ETH/USDT", "tf": "15m", "close": 2000.0, "dd": -0.03, "rsi": 25}
    mod._paper_open(sig, "RANGING")
    assert not paper.exists() or not json.loads(paper.read_text())["open"]


def test_pullback_opener_respects_disable(trader, monkeypatch):
    mod, state, paper = trader
    _write_state(state, ["pullback"])
    # Force the pure signal test True so we exercise only the disable gate.
    monkeypatch.setattr(mod, "_is_pullback", lambda sig: True)
    sig = {"symbol": "SOL/USDT", "tf": "15m", "close": 100.0}
    assert mod._pullback_open(sig, "RANGING") is False
    assert not paper.exists() or not json.loads(paper.read_text())["open"]
