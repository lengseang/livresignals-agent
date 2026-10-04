"""Gold scanner stale-data guard (2026-10-04).

Yahoo's chart API sometimes serves its last good window with the newest bars
nulled out; fetch_15m drops nulls, leaving the scanner evaluating ~9h-old
prices against a frozen print. scan() must refuse to act on data whose newest
bar is older than MAX_BAR_AGE_MIN.
"""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "gold_scanner", Path(__file__).resolve().parents[1] / "scripts" / "gold_scanner.py"
)
gs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gs)


def _bars(newest_age_min: float, n: int = 120):
    """n 15m bars whose newest timestamp is `newest_age_min` minutes ago."""
    now = time.time()
    step = 900
    start = now - newest_age_min * 60 - (n - 1) * step
    return [{"t": start + i * step, "c": 100.0 + i, "h": 101.0 + i, "l": 99.0 + i}
            for i in range(n)]


def test_stale_bars_refused(monkeypatch):
    monkeypatch.setattr(gs, "fetch_15m", lambda n=300: _bars(gs.MAX_BAR_AGE_MIN + 5))
    assert gs.scan() is None


def test_fresh_bars_accepted(monkeypatch):
    monkeypatch.setattr(gs, "fetch_15m", lambda n=300: _bars(5))
    st = gs.scan()
    assert st is not None and st["px"] > 0


def test_too_few_bars_refused(monkeypatch):
    monkeypatch.setattr(gs, "fetch_15m", lambda n=300: _bars(5, n=30))
    assert gs.scan() is None
