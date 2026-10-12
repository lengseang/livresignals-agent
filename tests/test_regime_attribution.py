"""Paper openers must persist the Judge's regime on every trade.

Regression test for the gap found 2026-10-10: `improvement_loop.py` reported
`by_regime` as a single "?" bucket holding all 2778 trades (0/2778 carried a
`regime`), so per-regime decay analysis was DISABLED and the loop could only
emit an honest `regime_data_gap` note. The Judge computes a real regime every
cycle; the paper openers simply never wrote it down.

The fix stamps `regime` onto each paper record. These tests pin it, and pin
that the sampler uses the `"?"` sentinel (never a fabricated regime) when no
regime is supplied — so a missing regime stays visibly missing.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# `unified_trader` is a real-money module on tests/loop/conftest.py's
# BLOCKED_MODULES list; that guard FAILS any loop test if the module is already
# resident in sys.modules. Importing it at module scope (the original form)
# poisoned all 341 tests/loop/ tests whenever the full suite ran together.
# Load it once here, but let each test evict it so collection order is
# irrelevant. See _load_ut().
_SPEC = importlib.util.spec_from_file_location(
    "unified_trader", ROOT / "scripts" / "unified_trader.py")


def _load_ut():
    """Fresh, isolated unified_trader import; caller must evict it after use."""
    ut = importlib.util.module_from_spec(_SPEC)
    sys.modules["unified_trader"] = ut
    _SPEC.loader.exec_module(ut)
    return ut


def _sig():
    # A minimal, internally-consistent signal dict the openers accept.
    return {"symbol": "BTC/USDT", "tf": "15m", "close": 100.0,
            "dd": -0.02, "rsi": 28.0, "mom": -1.0}


@pytest.fixture()
def ut(tmp_path, monkeypatch):
    """Loaded unified_trader with PAPER_STATE + ROOT redirected to a temp dir.

    Evicts the module from sys.modules on teardown so tests/loop/'s
    BLOCKED_MODULES guard (which fails on a resident real-money module) is
    never tripped regardless of collection order.
    """
    was_present = "unified_trader" in sys.modules
    mod = _load_ut()
    try:
        data = tmp_path / "data"
        data.mkdir(exist_ok=True)
        (data / "signals-state.json").write_text(
            json.dumps({"disabled": [], "promoted": [], "disabled_symbols": []}))
        p = tmp_path / "unified-paper.json"
        p.write_text(json.dumps({"open": [], "closed": []}))
        monkeypatch.setattr(mod, "PAPER_STATE", p)
        # Isolate ROOT so `_edge_disabled` (2026-10-12) can never read the live
        # signals-state.json — these tests pin the regime stamp, not the
        # disable list (which currently disables 'loose'/'validated').
        monkeypatch.setattr(mod, "ROOT", tmp_path)
        yield mod, p
    finally:
        if not was_present:
            sys.modules.pop("unified_trader", None)


def test_paper_open_stamps_the_judge_regime(ut):
    mod, p = ut
    mod._paper_open(_sig(), regime="bull")
    rec = json.loads(p.read_text())["open"][0]
    assert rec["regime"] == "bull", rec


def test_paper_loose_open_stamps_the_judge_regime(ut):
    mod, p = ut
    # loose needs dd<=-0.011 and rsi<=58 — the _sig above qualifies.
    mod._paper_loose_open(_sig(), regime="bear")
    rec = json.loads(p.read_text())["open"][0]
    assert rec["regime"] == "bear", rec


def test_missing_regime_is_the_sentinel_not_a_fabricated_one(ut):
    """No regime supplied -> '?' (visibly missing), never a guess."""
    mod, p = ut
    mod._paper_open(_sig())  # no regime kwarg
    rec = json.loads(p.read_text())["open"][0]
    assert rec["regime"] == "?", rec


def test_openers_still_work_without_the_regime_argument(ut):
    """Defaulted param: existing callers/tests must not break."""
    mod, p = ut
    mod._paper_open(_sig())
    sig2 = _sig()
    sig2["tf"] = "30m"  # distinct (symbol, tf) so the dup-guard allows it
    mod._paper_loose_open(sig2)
    assert len(json.loads(p.read_text())["open"]) == 2
