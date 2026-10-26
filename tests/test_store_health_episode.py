"""The store health gate must count EPISODES, not the series that happen to be
dead at the moment it is asked.

Measured 2026-10-25 09:00-13:00 UTC on the live store `data/trading.db`:

    data-harness (daily 08:00)   candle store  OK  0.0h  10 symbols
    data-health.json (09:30)     ok_all: false  — ADA ARB AVAX BNB DOGE LINK XRP
                                 "0 bars in 1h, 5.9h behind the store"
    candle-ingest.log            "ingested 2145 candles across 15 series /
                                  OK — all series fresh"  (every 15 min all night)

Three monitors, three answers, and the one with a non-zero exit (the coverage
gate) was not scheduled. Two separate defects, both about what a number is
allowed to mean:

1. `data_harness` proves a sqlite source is alive by `COUNT(*)` — 9.6M rows of
   history are a large number whatever happened in the last hour. Its age comes
   from `MAX(open_time_utc)` over the whole table, i.e. from whichever symbol
   most recently had a bar. That is the same aggregate-blind-spot the coverage
   gate in `trading.data.health` was built to close, still open one monitor
   over: the harness is the one that runs, and it said OK.

2. Coverage is a per-run verdict. An ingest outage is an *episode* with a start
   and (sometimes) an end; a per-run count cannot say whether the store is in
   its first stale hour or its twelfth, cannot say whether the outage that
   started at 04:00 is still running, and cannot distinguish "one late symbol
   for 20 minutes" ("8 holes in 3h") from "seven symbols dead since last
   night" — both are `covered: false`. `data-health.json` is rewritten every
   run, so this morning's 6h outage becomes a single boolean at 13:00 and the
   next run has no memory that it happened.

Each test below fails against the pre-fix code.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from trading.data.normalize import normalize_candle

from trading.data.health import check_data_health, series_coverage

try:                       # RED until the gate names episodes (2026-10-25)
    from trading.data.health import series_episodes
except ImportError:        # pragma: no cover - pre-fix module
    def series_episodes(*a, **k):
        raise AssertionError("health.series_episodes does not exist yet")


from trading.data.store import init, upsert_candle

IV = {"5m": 300_000, "30m": 1800_000, "1h": 3600_000, "4h": 14400_000, "1d": 86400_000}
CORE = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]


def _candle(symbol, tf, open_ms, close=100.0):
    """A store row. Built through normalize_candle: upsert keyed on
    open_time_utc > 0 is a no-op, which silently made an earlier version of
    this file seed nothing."""
    iv = IV[tf]
    # normalize_candle REFUSES a bar whose window is still open (that is its
    # job), so a back-dated row must be normalised as if it had just closed.
    raw = [open_ms, close - 1, close + 1, close - 2, close,
           1.0, open_ms + iv - 1, 1.0, 1, 0.5, 0.5, 0]
    row = normalize_candle(raw, symbol, tf, source="test",
                           now_ms=open_ms + iv)
    assert row and row["open_time_utc"] == open_ms, (symbol, tf, open_ms)
    return row


def _commit(con):
    """Durability boundary. `upsert_candle` does not commit; a read-only
    connection in WAL mode sees only committed frames, so a seed that is not
    committed is a seed the gate legitimately cannot see (it read an empty
    store and said so — correct behaviour, wrong fixture)."""
    con.commit()


def _seed_dense(con, sym, tf, start_ms, hours, close=100.0):
    """Contiguous bars starting at start_ms for `hours`."""
    iv = IV[tf]
    n = int(hours * 3600_000 // iv)
    for k in range(n):
        row = _candle(sym, tf, start_ms + k * iv, close)
        assert upsert_candle(con, row, now_ms=row["close_time_utc"] + 1)
    _commit(con)


def _seed_core_now(con, now_ms):
    for sym in CORE:
        for tf, iv in IV.items():
            row = _candle(sym, tf, (now_ms // iv) * iv - 2 * iv)
            assert upsert_candle(con, row, now_ms=row["close_time_utc"] + 1)
    _commit(con)


# --------------------------------------------------------------------------
# 1. the harness aggregate
# --------------------------------------------------------------------------

def _harness_mod(name="data_harness_under_test"):
    """Load scripts/data_harness.py as a module (it is a script, not a pkg)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        name, "/opt/trading-agent/scripts/data_harness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _harness_row(db_path):
    """Run the harness reader for the sqlite kind: (rows, note)."""
    return _harness_mod().count_rows(Path(db_path), "sqlite")


def test_a_big_history_does_not_prove_the_store_is_alive(tmp_path):
    """9.6M rows and nothing for 10h is a dead feed, not a healthy source."""
    db = tmp_path / "h.db"
    con = init(db)
    now = int(time.time() * 1000)
    # ten symbols with a long history that stops 10h ago
    for sym in CORE:
        _seed_dense(con, sym, "5m", now - 34 * 3600_000, 24)
    _seed_dense(con, "ADA/USDT", "5m", now - 34 * 3600_000, 24)
    _commit(con)
    con.close()

    rows, note = _harness_row(db)
    assert rows >= 1000                      # the count alone passes the floor
    # the verdict that matters: the harness must be able to say the data is old
    age_h, series_note = _harness_age(db, with_note=True)
    assert age_h >= 9.5, f"harness reported age {age_h}h for a store dead 10h"
    assert "stale" in series_note


def test_the_harness_age_is_not_the_newest_symbol():
    """MAX over the table = whichever symbol most recently had a bar."""
    # the live store on 2026-10-25 09:00: BTC/ETH/SOL current, 7 alts 5.9h dead
    db = "/opt/trading-agent/data/trading.db"
    import os
    if not os.path.exists(db):
        pytest.skip("live store not present")
    age_h, note = _harness_age(db, with_note=True)
    cov = series_coverage(db)
    dead = [s for s, c in cov.items() if not c["covered"]]
    if not dead:
        pytest.skip("no uncovered series to contrast with right now")
    # if any series is dead, the store-wide age may not read as fresh
    oldest_dead = max(cov[s]["cover_lag_h"] for s in dead)
    assert age_h >= min(oldest_dead, 5.0) or "not covered" in note.lower(), (
        f"harness age {age_h}h while {len(dead)} series are up to {oldest_dead}h dead")


def _harness_age(db_path, with_note=False):
    """The age the harness will print for the candle store."""
    out = _harness_mod("data_harness_under_test2").sqlite_series_age(Path(db_path))
    return out if with_note else out[0]


def test_the_harness_prints_the_darkest_series_not_the_file_age(tmp_path, capsys):
    """A store touched now, whose alts are 10h dead, must not print OK 0.0h.

    The helper-level tests above pin `sqlite_series_age`. This pins the thing
    the owner actually reads: the AGE COLUMN and the VERDICT `check()` prints.
    A mutation that swaps the helper back to the file mtime passes every other
    test in this file and fails only here.
    """
    db = tmp_path / "h.db"
    con = init(db)
    now = int(time.time() * 1000)
    for sym in CORE:
        _seed_dense(con, sym, "5m", now - 34 * 3600_000, 24)
    _seed_dense(con, "ADA/USDT", "5m", now - 34 * 3600_000, 24)   # dies 10h ago
    _commit(con)
    con.close()

    mod = _harness_mod("data_harness_checkpaths")
    mod.MANIFEST = [("candle store (sqlite)", Path(db), "sqlite", 6, 1000, "test")]
    mod.check()                     # writes data-manifest.json: restore after
    out = capsys.readouterr().out
    line = [ln for ln in out.splitlines() if "candle store" in ln][0]
    assert "STALE" in line, line        # verdict, not just an age
    assert "10." in line, line          # the darkest series' age, not 0.0h
    assert "ADA" in line, line          # and it names which one


@pytest.fixture(autouse=True)
def _live_manifest_untouched():
    """`check()` persists data/data-manifest.json. Snapshot and restore it so a
    test cannot rewrite the artefact the daily cron reports on."""
    p = Path("/opt/trading-agent/data/data-manifest.json")
    before = p.read_bytes() if p.exists() else None
    yield
    if before is None:
        p.unlink(missing_ok=True)
    else:
        p.write_bytes(before)


# --------------------------------------------------------------------------
# 2. coverage as an episode, not a boolean
# --------------------------------------------------------------------------

def test_an_ongoing_outage_is_reported_with_its_start(tmp_path):
    """Seven alts dead since 03:25, asked at 13:00: one episode, still open."""
    db = tmp_path / "h.db"
    con = init(db)
    now = int(time.time() * 1000)
    _seed_core_now(con, now)
    stop = now - 10 * 3600_000
    for sym in ("ADA/USDT", "ARB/USDT", "AVAX/USDT"):
        _seed_dense(con, sym, "5m", stop - 14 * 3600_000, 14)
        _seed_dense(con, sym, "1h", stop - 14 * 3600_000, 14)
        _seed_dense(con, sym, "1d", stop - 30 * 24 * 3600_000, 72)
    con.close()

    eps = series_episodes(db)
    open_eps = [e for e in eps["episodes"] if e["end_utc"] is None]
    assert len(open_eps) == 1, f"expected one ongoing episode, got {len(open_eps)}"
    ep = open_eps[0]
    assert ep["symbols"] == ["ADA/USDT", "ARB/USDT", "AVAX/USDT"]
    since = datetime.strptime(ep["since_utc"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    assert since.timestamp() < time.time() - 9 * 3600, ep["since_utc"]
    assert ep["hours_open" if "hours_open" in ep else "duration_h"] >= 9.5


def test_one_late_symbol_20_minutes_ago_is_not_the_same_thing(tmp_path):
    """A per-run boolean cannot tell 20 minutes from 10 hours. The episode can."""
    db = tmp_path / "h.db"
    con = init(db)
    now = int(time.time() * 1000)
    _seed_core_now(con, now)
    # ADA stopped 0.5h ago, everyone else current
    stop = now - 30 * 60_000
    _seed_dense(con, "ADA/USDT", "5m", stop - 12 * 3600_000, 12)
    _seed_dense(con, "ADA/USDT", "1h", stop - 12 * 3600_000, 12)
    _seed_dense(con, "ADA/USDT", "1d", stop - 30 * 24 * 3600_000, 72)
    con.close()

    eps = series_episodes(db, min_minutes=2)
    assert eps["open_hours_max"] <= 1.0, (
        f"a 30-minute gap reported as {eps['open_hours_max']}h")
    assert eps["open_symbols"] >= 1


def test_the_coverage_verdict_names_the_episode_not_just_the_series(tmp_path):
    """check_data_health must carry the episode in its problems/report."""
    db = tmp_path / "h.db"
    con = init(db)
    now = int(time.time() * 1000)
    _seed_core_now(con, now)
    stop = now - 8 * 3600_000
    _seed_dense(con, "ADA/USDT", "5m", stop - 12 * 3600_000, 12)
    _seed_dense(con, "ADA/USDT", "1h", stop - 12 * 3600_000, 12)
    _seed_dense(con, "ADA/USDT", "1d", stop - 30 * 24 * 3600_000, 72)
    con.close()

    rep = check_data_health(db, coverage=True)
    assert rep["ok_all"] is False
    assert "episodes" in rep, "the report has no memory of when the outage began"
    assert rep["episodes"]["open_symbols"] >= 1
    joined = " ".join(rep["problems"])
    assert "since" in joined.lower() or "episode" in joined.lower()


def test_the_live_store_can_answer_how_long_the_alts_have_been_dark():
    """The real question asked on 2026-10-25: is this a new outage or the old one?"""
    import os
    db = "/opt/trading-agent/data/trading.db"
    if not os.path.exists(db):
        pytest.skip("live store not present")
    eps = series_episodes(db)
    assert "episodes" in eps and isinstance(eps["episodes"], list)
    for e in eps["episodes"]:
        assert e["since_utc"] is not None
        assert e["symbols"]
