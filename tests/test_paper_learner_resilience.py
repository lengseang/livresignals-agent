"""paper_learner data-source resilience (2026-10-05).

The daily 10:00 paper-learner cron crashed for days with FileNotFoundError on
data/full_sweep.json (deleted by disk_cleanup.sh as "referenced NOWHERE", but
paper_learner DOES read it). Two regressions to pin down:

  1. _load_all_series() must fall back to live_fine.json / alts_fine.json and
     return {} (never raise) when no source is readable.
  2. exactly one evaluate_params exists, and it accepts the cached series arg.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "paper_learner", Path(__file__).resolve().parents[1] / "scripts" / "paper_learner.py"
)
pl = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pl)


def test_load_series_missing_file_does_not_raise(monkeypatch, tmp_path):
    """A missing sweep file must degrade to {} — never crash the cron."""
    monkeypatch.setattr(pl, "SWEEP", tmp_path / "nope.json")
    monkeypatch.setattr(pl, "DATA", tmp_path)
    assert pl._load_all_series() == {}


def test_load_series_reads_fallback(monkeypatch, tmp_path):
    """Bare-timeframe keys in a fallback file map onto SERIES_KEYS."""
    (tmp_path / "live_fine.json").write_text('{"15m": [[1, 100.0], [2, 101.0]]}')
    monkeypatch.setattr(pl, "SWEEP", tmp_path / "missing.json")
    monkeypatch.setattr(pl, "DATA", tmp_path)
    out = pl._load_all_series()
    assert out.get("15m:BTC/USDT") == [100.0, 101.0]


def test_best_params_returns_none_without_data(monkeypatch, tmp_path):
    """No data -> best_params logs and returns None instead of raising."""
    monkeypatch.setattr(pl, "SWEEP", tmp_path / "missing.json")
    monkeypatch.setattr(pl, "DATA", tmp_path)
    monkeypatch.setattr(pl, "STATE", tmp_path / "state.json")
    assert pl.best_params() is None


def test_evaluate_params_accepts_series_cache():
    """The surviving definition must take the cached series arg (4-arity)."""
    import inspect

    sig = inspect.signature(pl.evaluate_params)
    assert len(sig.parameters) == 4
