"""Regression tests for the futures research pause (2026-10-06).

The SOL/XRP 15m sharp-dip entry was proven negative-EV in BOTH out-of-sample
halves across 4 independent tests (~3000 real 15m bars/symbol, fee 0.08%).
The pause blocks NEW entries only; management of open positions is untouched.
These tests pin: (1) the gate reads signals-state.json, (2) both entry paths
(futures_open AND futures_scan) honour it, (3) it fails OPEN on a bad state
file so a corrupt file can never silently stall the venue.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load():
    spec = importlib.util.spec_from_file_location(
        "futures_executor_t", ROOT / "scripts" / "futures_executor.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def fe(tmp_path, monkeypatch):
    m = _load()
    state = tmp_path / "signals-state.json"
    monkeypatch.setattr(m, "SIGNALS_STATE", state)
    return m, state


def test_pause_true_when_flag_set(fe):
    m, state = fe
    state.write_text(json.dumps({"futures_sharp_dip_paused": True}))
    assert m.sharp_dip_paused() is True


def test_pause_false_when_flag_absent(fe):
    m, state = fe
    state.write_text(json.dumps({"disabled": ["breakout"]}))
    assert m.sharp_dip_paused() is False


def test_pause_fails_open_on_corrupt_file(fe):
    m, state = fe
    state.write_text("{not json")
    assert m.sharp_dip_paused() is False


def test_pause_fails_open_on_missing_file(fe):
    m, _state = fe
    assert m.sharp_dip_paused() is False


def test_futures_open_refuses_when_paused(fe, monkeypatch):
    m, state = fe
    state.write_text(json.dumps({"futures_sharp_dip_paused": True}))
    # would otherwise reach the network; must refuse before that
    r = m.futures_open("SOL/USDT", {"rsi": 30, "dd": -0.02})
    assert r["ok"] is False
    assert "PAUSED" in r["err"]


def test_futures_scan_refuses_when_paused(fe, monkeypatch):
    m, state = fe
    state.write_text(json.dumps({"futures_sharp_dip_paused": True}))
    monkeypatch.setattr(m, "_load", lambda: {"open": [], "closed": []})
    monkeypatch.setattr(m, "_daily_loss", lambda: 0.0)
    r = m.futures_scan()
    assert r.get("opened") == 0
    assert "PAUSED" in r.get("reason", "")


def test_risk_envelope_unchanged():
    """The pause must not have touched leverage or the stop."""
    m = _load()
    assert m.LEVERAGE == 3
    assert m.DAILY_LOSS_CAP == 1.00
