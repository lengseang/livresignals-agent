"""paper_learner: the verdict must state WHAT DATA it was computed on (2026-10-20).

THE DEFECT THIS PINS DOWN
-------------------------
The learner scored its 72-config grid on FROZEN sweep files
(data/live_fine.json + data/alts_fine.json, both last written 2026-08-15)
while the live candle DB carried data to today. The verdict it printed was:

    best found (NET of fees): dip=-0.08 rsi<=30 red>=4 acc=56.3% net_ev=+0.184% n=907

which reads as a claim about the CURRENT market. It is a claim about
2023-01-01..2026-08-15. Downstream (self_improve, the improvement report,
night_tuner) consumed "net_ev=+0.184%" and reasoned about today from it.

This is SYSTEM_MAP rule 11's corollary in a new place: a derived verdict
narrated without its provenance. A number is only as trustworthy as the
window it was measured over, and the window was invisible.

These tests lock in:
  1. _load_all_series stamps every series with the file it came from + mtime.
  2. the stamp tracks the file, not a hardcoded string (rule 9).
  3. both stored shapes ([(ts, close), ...] and bare [close, ...]) load,
     because x[1] on a bare float used to raise OUTSIDE the try and kill
     the whole grid.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "paper_learner", Path(__file__).resolve().parents[1] / "scripts" / "paper_learner.py"
)
pl = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pl)


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_series_are_stamped_with_source_file(tmp_path, monkeypatch):
    """Every loaded series records which file and which mtime it came from."""
    live = tmp_path / "live_fine.json"
    alts = tmp_path / "alts_fine.json"
    _write(live, {"15m": [[i, 100.0 + i] for i in range(200)]})
    _write(alts, {"15m:ETH/USDT": [[i, 50.0 + i] for i in range(200)]})

    monkeypatch.setattr(pl, "DATA", tmp_path)
    monkeypatch.setattr(pl, "SWEEP", tmp_path / "full_sweep.json")  # absent
    pl._LOAD_STAMP.clear()

    series = pl._load_all_series()
    assert series, "expected at least one series from the fallback files"

    for key in series:
        assert key in pl._LOAD_STAMP, f"{key} loaded without a provenance stamp"
        fname, mtime = pl._LOAD_STAMP[key]
        assert mtime > 0
        assert fname in {"live_fine.json", "alts_fine.json"}

    # the BTC key came from live_fine, the ETH key from alts_fine
    assert pl._LOAD_STAMP["15m:BTC/USDT"][0] == "live_fine.json"
    assert pl._LOAD_STAMP["15m:ETH/USDT"][0] == "alts_fine.json"


def test_stamp_reflects_the_file_not_a_constant(tmp_path, monkeypatch):
    """Touching the source file must move the stamp — it is re-derived, not quoted."""
    live = tmp_path / "live_fine.json"
    _write(live, {"15m": [[i, 100.0 + i] for i in range(200)]})
    monkeypatch.setattr(pl, "DATA", tmp_path)
    monkeypatch.setattr(pl, "SWEEP", tmp_path / "full_sweep.json")

    pl._LOAD_STAMP.clear()
    pl._load_all_series()
    first = pl._LOAD_STAMP["15m:BTC/USDT"][1]

    _write(live, {"15m": [[i, 100.0 + i] for i in range(200)]})
    live.touch()
    import os
    os.utime(live, (first + 500, first + 500))

    pl._LOAD_STAMP.clear()
    pl._load_all_series()
    second = pl._LOAD_STAMP["15m:BTC/USDT"][1]
    assert second != first, "stamp is a constant, not the file's real mtime"


def test_bare_close_lists_do_not_kill_the_grid(tmp_path, monkeypatch):
    """A source storing bare closes must be normalised, not raise TypeError.

    Regression: `[x[1] for x in series]` on [float, ...] raises
    "'float' object is not subscriptable" BEFORE the try/except, so a single
    bare-list source took down every series and the whole search.
    """
    bare = tmp_path / "live_fine.json"
    _write(bare, {"15m": [100.0 + i for i in range(300)]})
    monkeypatch.setattr(pl, "DATA", tmp_path)
    monkeypatch.setattr(pl, "SWEEP", tmp_path / "full_sweep.json")

    pl._LOAD_STAMP.clear()
    series = pl._load_all_series()   # must not raise
    assert "15m:BTC/USDT" in series
    assert series["15m:BTC/USDT"][:3] == [100.0, 101.0, 102.0]


def test_missing_sources_do_not_raise(tmp_path, monkeypatch):
    """No sources at all -> {} and a clean skip, never a traceback (cron safety)."""
    monkeypatch.setattr(pl, "DATA", tmp_path)
    monkeypatch.setattr(pl, "SWEEP", tmp_path / "full_sweep.json")
    pl._LOAD_STAMP.clear()
    assert pl._load_all_series() == {}
    assert pl.best_params() is None
