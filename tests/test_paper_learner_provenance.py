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
    monkeypatch.setattr(pl, "SWEEP", tmp_path / "full_sweep.json")
    monkeypatch.setattr(pl, "DB", tmp_path / "no-such.db")  # live store must not leak into a fixture  # absent
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
    monkeypatch.setattr(pl, "DB", tmp_path / "no-such.db")  # live store must not leak into a fixture

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
    monkeypatch.setattr(pl, "DB", tmp_path / "no-such.db")  # live store must not leak into a fixture

    pl._LOAD_STAMP.clear()
    series = pl._load_all_series()   # must not raise
    assert "15m:BTC/USDT" in series
    assert series["15m:BTC/USDT"][:3] == [100.0, 101.0, 102.0]


def test_missing_sources_do_not_raise(tmp_path, monkeypatch):
    """No sources at all -> {} and a clean skip, never a traceback (cron safety)."""
    monkeypatch.setattr(pl, "DATA", tmp_path)
    monkeypatch.setattr(pl, "SWEEP", tmp_path / "full_sweep.json")
    monkeypatch.setattr(pl, "DB", tmp_path / "no-such.db")  # live store must not leak into a fixture
    pl._LOAD_STAMP.clear()
    assert pl._load_all_series() == {}
    assert pl.best_params() is None


# ── 2026-10-20 (part 2): the learner now SCORES the live DB ──────────────────
# Provenance alone was not the fix. The grid was still computed on a file
# frozen 65 days earlier, so the verdict described a market that no longer
# existed. These pin the source ORDER and that the printed window is derived
# from the bars actually scored.

def _make_db(path, rows):
    """rows: {("BTC/USDT","15m"): [(ms, close), ...]}"""
    import sqlite3
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE candles (symbol TEXT, timeframe TEXT, "
                "open_time_utc INTEGER, close REAL, PRIMARY KEY "
                "(symbol, timeframe, open_time_utc))")
    for (sym, tf), pts in rows.items():
        con.executemany("INSERT INTO candles VALUES (?,?,?,?)",
                        [(sym, tf, ts, c) for ts, c in pts])
    con.commit()
    con.close()


def test_db_is_preferred_over_a_frozen_sweep_file(tmp_path, monkeypatch):
    """Fresh DB bars must win over the frozen file for the same series."""
    db = tmp_path / "trading.db"
    _make_db(db, {("BTC/USDT", "15m"): [(1000 + i, 200.0 + i) for i in range(400)]})
    # frozen file also has 15m BTC, with a DIFFERENT price level
    _write(tmp_path / "live_fine.json",
           {"15m": [[i, 100.0 + i] for i in range(300)]})
    monkeypatch.setattr(pl, "DATA", tmp_path)
    monkeypatch.setattr(pl, "SWEEP", tmp_path / "full_sweep.json")
    monkeypatch.setattr(pl, "DB", db)

    series = pl._load_all_series()
    assert pl._LOAD_STAMP["15m:BTC/USDT"][0] == "trading.db"
    # DB values, not file values
    assert series["15m:BTC/USDT"][:3] == [200.0, 201.0, 202.0]
    # and the window is the DB's window, derived per run
    assert pl._LOAD_WINDOW["15m:BTC/USDT"] == (1000, 1399)


def test_sweep_file_still_serves_keys_the_db_cannot(tmp_path, monkeypatch):
    """A key absent from the DB must still fall back, and say it is frozen.

    Four SERIES_KEYS (BTC 5m/15m/30m, XRP 30m) exist only in the sweep files;
    dropping them silently would shrink the grid without anyone noticing.
    """
    db = tmp_path / "trading.db"
    _make_db(db, {("BTC/USDT", "15m"): [(1000 + i, 200.0 + i) for i in range(400)]})
    _write(tmp_path / "live_fine.json",
           {"5m": [[i, 100.0 + i] for i in range(300)]})
    monkeypatch.setattr(pl, "DATA", tmp_path)
    monkeypatch.setattr(pl, "SWEEP", tmp_path / "full_sweep.json")
    monkeypatch.setattr(pl, "DB", db)

    series = pl._load_all_series()
    assert pl._LOAD_STAMP["15m:BTC/USDT"][0] == "trading.db"
    assert pl._LOAD_STAMP["5m:BTC/USDT"][0] == "live_fine.json"
    assert series["5m:BTC/USDT"][:2] == [100.0, 101.0]


def test_best_params_scores_fresh_bars_a_frozen_file_would_hide(tmp_path, monkeypatch):
    """The verdict must respond to bars the frozen file does not contain.

    Builds a deep -10% crash with RSI <= 30 present ONLY in the DB. If the
    loader still preferred the (flat) frozen file, no config could fire.
    """
    import math
    pts, ts = [], 1_000_000
    px = 100.0
    for i in range(600):                     # 300 up-drift bars then a crash
        step = 0.4 if i < 300 else -0.30
        px = max(1.0, px * (1 + step / 100))
        pts.append((ts + i * 900_000, px))
    db = tmp_path / "trading.db"
    _make_db(db, {("BTC/USDT", "15m"): pts})
    _write(tmp_path / "live_fine.json",          # flat: yields no dip entry
           {"15m": [[i, 100.0 + 0.0001 * math.sin(i)] for i in range(600)]})
    monkeypatch.setattr(pl, "DATA", tmp_path)
    monkeypatch.setattr(pl, "SWEEP", tmp_path / "full_sweep.json")
    monkeypatch.setattr(pl, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(pl, "DB", db)

    pl._LOAD_STAMP.clear()
    series = pl._load_all_series()
    got = pl.simulate(series["15m:BTC/USDT"], -0.04, 45, 2, pl.HOLDS["15m"])
    assert got is not None, "fresh crash series produced no trades at all"


def test_untestable_winner_is_not_adopted(tmp_path, monkeypatch, capsys):
    """A config that wins on full-sample EV but never fires recently must not
    be adopted — it is a number about a market that no longer exists.

    The grid is driven directly (best_params is stubbed to return the real
    2026-10-20 winner); what is under test is the ADOPTION DECISION, not the
    grid. Synthetic price paths cannot out-earn the 0.1594% round-trip fee
    after a 6-bar hold, so building a genuine winner would test the simulator
    rather than this gate.
    """
    import time
    now = int(time.time() * 1000)
    span = 900_000
    start = now - 1100 * span
    # calm bars since ~90 days ago; the 90-day slice has no -8% drawdown
    pts = [(start + i * span, 100.0 * (1.0001 ** i)) for i in range(1100)]
    db = tmp_path / "trading.db"
    _make_db(db, {("BTC/USDT", "15m"): pts})
    monkeypatch.setattr(pl, "DATA", tmp_path)
    monkeypatch.setattr(pl, "SWEEP", tmp_path / "full_sweep.json")
    monkeypatch.setattr(pl, "DB", db)
    monkeypatch.setattr(pl, "STATE", tmp_path / "state.json")
    # the real winner: great full-sample EV, zero trades in 90 days
    monkeypatch.setattr(pl, "best_params",
                        lambda: (0.481, -0.08, 30, 4, 0.481, 2645, 59.1))

    pl.main()
    out = capsys.readouterr().out
    assert "RECENCY" in out, out
    assert "ZERO times" in out or "n=0" in out, out
    assert "untestable" in out, out
    state = json.loads((tmp_path / "state.json").read_text())
    assert "current_params" not in state, "adopted a config it just called untestable"
    assert state["untestable_best"]["recent_n_90d"] == 0


def test_a_config_that_trades_recently_IS_adopted(tmp_path, monkeypatch, capsys):
    """Control: the recency gate must still ADOPT a winner that clears 20
    trades in 90 days. A guard that blocks everything is indistinguishable
    from a broken one (SYSTEM_MAP rule 10 corollary: every check must be able
    to fail AND to pass).

    Drives `simulate` rather than inventing a price path: this test is about
    the DECISION (recent_n >= 20 -> adopt), and the real fixture hunt proved
    that hand-built paths are a poor way to reach it.
    """
    import time
    now = int(time.time() * 1000)
    span = 900_000
    pts = [(now - (1000 - i) * span, 100.0) for i in range(1000)]
    db = tmp_path / "trading.db"
    _make_db(db, {("BTC/USDT", "15m"): pts})
    monkeypatch.setattr(pl, "DATA", tmp_path)
    monkeypatch.setattr(pl, "SWEEP", tmp_path / "full_sweep.json")
    monkeypatch.setattr(pl, "DB", db)
    monkeypatch.setattr(pl, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(pl, "best_params",
                        lambda: (0.481, -0.01, 45, 2, 0.481, 2645, 59.1))
    # 40 recent trades at +1% net -> recent_n=40 -> testable
    monkeypatch.setattr(pl, "simulate", lambda *a, **k: (30, 10, 1.0))

    pl.main()
    out = capsys.readouterr().out
    state = json.loads((tmp_path / "state.json").read_text())
    assert "RECENCY" in out and "n=40" in out, out
    assert "ADOPTED" in out, out
    assert state["current_params"]["dip"] == -0.01


def test_the_recency_counter_matches_the_grid_on_real_bars():
    """The RECENCY line must count what the grid would count.

    Guards against the two lines drifting apart: the grid scores a whole
    series, the recency line scores a time-sliced one. On a dense synthetic
    series the trade COUNT from `simulate` must be reproducible by walking the
    same rule, so a future refactor cannot quietly report a different n.
    """
    pts, px = [], 100.0
    for i in range(1200):
        px *= 0.994 if i % 7 == 0 else 1.0004
        pts.append(px)
    got = pl.simulate(pts, -0.04, 45, 1, 4)
    assert got is not None, "fixture produced no trades — useless as a control"
    wins, losses, _ = got
    n = 0
    for i in range(90, len(pts) - 4):
        if pts[i] / pl._rolling_max(pts, i) - 1 > -0.04:
            continue
        if pl._rsi(pts, i) > 45:
            continue
        red, j = 0, i
        while j > 0 and pts[j] < pts[j - 1]:
            red += 1; j -= 1
        if red >= 1:
            n += 1
    assert n == wins + losses, f"walked {n} vs simulate {wins + losses}"
