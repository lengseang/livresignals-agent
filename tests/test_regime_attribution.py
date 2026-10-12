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

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "unified_trader", ROOT / "scripts" / "unified_trader.py")
ut = importlib.util.module_from_spec(_spec)
sys.modules["unified_trader"] = ut
_spec.loader.exec_module(ut)


def _sig():
    # A minimal, internally-consistent signal dict the openers accept.
    return {"symbol": "BTC/USDT", "tf": "15m", "close": 100.0,
            "dd": -0.02, "rsi": 28.0, "mom": -1.0}


def _redirect_state(tmp_path, monkeypatch):
    """Point the openers' PAPER_STATE at a fresh temp file.

    ALSO isolate ROOT so `_edge_disabled` (added 2026-10-12) can never read the
    live data/signals-state.json — these tests exercise the regime stamp, not
    the disable list, and the live list currently disables 'loose'/'validated'.
    """
    p = tmp_path / "unified-paper.json"
    p.write_text(json.dumps({"open": [], "closed": []}))
    monkeypatch.setattr(ut, "PAPER_STATE", p)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "signals-state.json").write_text(
        json.dumps({"disabled": [], "promoted": [], "disabled_symbols": []}))
    monkeypatch.setattr(ut, "ROOT", tmp_path)
    return p


def test_paper_open_stamps_the_judge_regime(tmp_path, monkeypatch):
    p = _redirect_state(tmp_path, monkeypatch)
    ut._paper_open(_sig(), regime="bull")
    rec = json.loads(p.read_text())["open"][0]
    assert rec["regime"] == "bull", rec


def test_paper_loose_open_stamps_the_judge_regime(tmp_path, monkeypatch):
    p = _redirect_state(tmp_path, monkeypatch)
    # loose needs dd<=-0.011 and rsi<=58 — the _sig above qualifies.
    ut._paper_loose_open(_sig(), regime="bear")
    rec = json.loads(p.read_text())["open"][0]
    assert rec["regime"] == "bear", rec


def test_missing_regime_is_the_sentinel_not_a_fabricated_one(tmp_path, monkeypatch):
    """No regime supplied -> '?' (visibly missing), never a guess."""
    p = _redirect_state(tmp_path, monkeypatch)
    ut._paper_open(_sig())  # no regime kwarg
    rec = json.loads(p.read_text())["open"][0]
    assert rec["regime"] == "?", rec


def test_openers_still_work_without_the_regime_argument(tmp_path, monkeypatch):
    """Defaulted param: existing callers/tests must not break."""
    p = _redirect_state(tmp_path, monkeypatch)
    ut._paper_open(_sig())
    sig2 = _sig()
    sig2["tf"] = "30m"  # distinct (symbol, tf) so the dup-guard allows it
    ut._paper_loose_open(sig2)
    assert len(json.loads(p.read_text())["open"]) == 2
