"""Regression test: a self-improve SYMBOL disable must actually stop the
REAL-money path from opening that symbol.

Defect found 2026-10-12: the self-improve engine stands symbols down via
data/signals-state.json['disabled_symbols']. `_symbol_disabled` is honoured by
the PAPER openers (`_paper_open`, `_paper_loose_open`, `_pullback_open` via the
guard at the top of each) but the REAL-money tier gate `_real_tier_ok` never
read it. That was latent while the list held only ARB (never ELITE) — the
docstring even says "Paper-only impact: ARB is never real". It became LIVE the
moment SOL/USDT was added, because SOL *is* in ELITE_SYMS: the real book has two
SOL closes on 2026-10-12 (04:16 STALL +0.0693, 11:56 TIME -0.018) while SOL sat
on the disabled list.

This is the same defect class as the edge-disable fixed the same day: a control
the system writes that the thing it claims to control does not read. These tests
pin `_real_tier_ok` (the single chokepoint every real-order call site passes
through) and `futures_open` (which the futures branch calls DIRECTLY).
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
    list; that guard fails any loop test if the module is already resident, so we
    import it scoped to this fixture and EVICT it on teardown.
    """
    sys.path.insert(0, str(SCRIPTS))
    was_present = "unified_trader" in sys.modules
    mod = importlib.import_module("unified_trader")
    try:
        data = tmp_path / "data"
        data.mkdir(exist_ok=True)
        monkeypatch.setattr(mod, "ROOT", tmp_path, raising=False)
        yield mod, data / "signals-state.json"
    finally:
        if not was_present:
            sys.modules.pop("unified_trader", None)


def _write_state(p: Path, disabled_symbols: list[str]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"disabled": [], "promoted": [],
                             "disabled_symbols": disabled_symbols}),
                 encoding="utf-8")


def _sharp_sig(sym: str) -> dict:
    # 15m SHARP signal: the only shape that qualifies for real money at all.
    return {"symbol": sym, "tf": "15m", "close": 100.0, "sharp": True,
            "dd": -0.02, "rsi": 30, "mom": 0.0}


def test_real_tier_ok_blocks_disabled_symbol(trader):
    mod, state = trader
    _write_state(state, ["SOL/USDT"])
    # SOL is ELITE and the signal is SHARP, so ONLY the disable should block it.
    assert mod._symbol_disabled("SOL/USDT") is True
    assert mod._real_tier_ok("SOL/USDT", False, _sharp_sig("SOL/USDT"), None) is False


def test_real_tier_ok_allows_non_disabled_elite(trader):
    mod, state = trader
    _write_state(state, ["SOL/USDT"])
    # ETH is elite, sharp, and NOT disabled -> must still be allowed.
    assert mod._real_tier_ok("ETH/USDT", False, _sharp_sig("ETH/USDT"), None) is True


def test_real_tier_ok_missing_state_is_failopen(trader):
    mod, state = trader
    if state.exists():
        state.unlink()
    # A missing/unreadable state file must NOT block real trading.
    assert mod._real_tier_ok("SOL/USDT", False, _sharp_sig("SOL/USDT"), None) is True


def test_real_tier_ok_clears_when_symbol_removed(trader):
    mod, state = trader
    _write_state(state, [])                      # nothing disabled
    assert mod._real_tier_ok("SOL/USDT", False, _sharp_sig("SOL/USDT"), None) is True


def test_futures_open_blocks_disabled_symbol(tmp_path, monkeypatch):
    """futures_open() is called DIRECTLY by unified_trader's futures branch, so
    it must enforce the disable itself — before any network call."""
    sys.path.insert(0, str(SCRIPTS))
    was_present = "futures_executor" in sys.modules
    mod = importlib.import_module("futures_executor")
    try:
        state = tmp_path / "signals-state.json"
        state.write_text(json.dumps({"disabled_symbols": ["SOL/USDT"]}),
                         encoding="utf-8")
        monkeypatch.setattr(mod, "SIGNALS_STATE", state, raising=False)

        def _boom(*a, **k):                      # must never be reached
            raise AssertionError("futures_open hit the network for a disabled symbol")
        monkeypatch.setattr(mod, "_daily_loss", lambda: 0.0, raising=False)
        monkeypatch.setattr(mod, "make_futures_exchange", _boom, raising=False)

        r = mod.futures_open("SOL/USDT", _sharp_sig("SOL/USDT"))
        assert r["ok"] is False
        assert "disabled_symbols" in r["err"]
    finally:
        if not was_present:
            sys.modules.pop("futures_executor", None)
