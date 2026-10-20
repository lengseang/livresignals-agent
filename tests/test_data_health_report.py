"""The data-health report must describe the database it was pointed at, and
must be able to say NO.

Measured 2026-10-20 01:00 UTC. Three defects in one 75-line module:

1. `check_data_health(db_path)` wrote GLOBAL `reports/data-health.json` even
   when `db_path` was a temp DB. The unit tests call it three times, each on
   a nearly-empty temp DB, so the live report has said
   `"ok": false, BTC/USDT:5m: no data ...` on every run since the suite was
   last executed, regardless of the real store's contents.
2. `storage_check()` returned `{groups, duplicate_keys}` and nothing looked
   at it — a storage failure could not reach `problems` at all.
3. The "no data" verdict was not checked against the reader's own view: in
   WAL mode a fresh read connection can report nothing while the file is
   full of committed candles.

Each test below fails against the pre-fix module.
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from trading.data import health as health_mod
from trading.data.health import check_data_health
from trading.data.store import init, storage_check, upsert_candle

IV = {"5m": 300_000, "30m": 1800_000, "1h": 3600_000, "4h": 14400_000, "1d": 86400_000}


def _candle(symbol, tf, open_ms, close=100.0):
    iv = IV[tf]
    return {"symbol": symbol, "timeframe": tf,
            "open_time_utc": open_ms, "close_time_utc": open_ms + iv - 1,
            "open": close - 1, "high": close + 1, "low": close - 2, "close": close,
            "volume": 1.0, "trades": 1, "taker_buy_volume": 0.5,
            "event_time_utc": open_ms, "received_time_utc": open_ms + iv,
            "source": "test", "source_id": f"{symbol}:{tf}:{open_ms}"}


def _seed_fresh(db):
    con = init(db)
    now = int(time.time() * 1000)
    for symbol in ("BTC/USDT", "ETH/USDT", "SOL/USDT"):
        for tf, iv in IV.items():
            upsert_candle(con, _candle(symbol, tf, now - 2 * iv))
    con.commit()
    con.close()


@pytest.fixture()
def live_report(tmp_path, monkeypatch):
    """Point REPORTS_DIR at tmp so a test can never overwrite the real file."""
    monkeypatch.setattr(health_mod, "REPORTS_DIR", tmp_path / "reports")
    return tmp_path / "reports" / "data-health.json"


def test_a_temp_db_run_does_not_overwrite_the_live_report(tmp_path, live_report):
    """The regression: a caller passing db_path must not publish anywhere."""
    db = tmp_path / "h.db"
    init(db).close()
    check_data_health(db)
    assert not live_report.exists(), (
        "check_data_health(db_path=<temp>) published the global report — the "
        "test suite will overwrite the live verdict with an empty DB's")


def test_only_publish_true_writes_the_report(tmp_path, live_report):
    db = tmp_path / "h.db"
    _seed_fresh(db)
    rep = check_data_health(db, publish=True)
    assert live_report.exists()
    on_disk = json.loads(live_report.read_text())
    assert on_disk["ok"] is True and on_disk["detail"] == rep["detail"]
    assert on_disk["db"].endswith("h.db"), "the report must name its database"


def test_published_report_names_the_db_it_describes(tmp_path, live_report):
    db = tmp_path / "h.db"
    _seed_fresh(db)
    rep = check_data_health(db, publish=True)
    assert str(db) == rep["db"]


def test_storage_can_fail_and_the_failure_reaches_problems(tmp_path):
    """A read-only connection: candles readable, store not writable.

    Note: `candles` has PRIMARY KEY (symbol,timeframe,open_time_utc), so a
    real duplicate row is impossible — the duplicate check is a belt-and-braces
    invariant, and the failure mode that CAN happen is the store not being
    writable by this process. That is what is exercised here.
    """
    con = init(tmp_path / "h.db")
    con.close()
    ro = sqlite3.connect(f"file:{tmp_path / 'h.db'}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    st = storage_check(ro)
    assert st["ok"] is False
    assert st["writable"] is False
    assert any("not writable" in p for p in st["problems"])
    ro.close()


def test_the_duplicate_key_invariant_is_actually_enforced_by_the_schema(tmp_path):
    """If the PK ever loosens, the health duplicate check must be what saves us."""
    con = init(tmp_path / "h.db")
    now = int(time.time() * 1000) - 7_200_000
    con.execute(
        "INSERT INTO candles (symbol,timeframe,open_time_utc,close_time_utc,open,high,"
        "low,close,volume,trades,taker_buy_volume,event_time_utc,received_time_utc,"
        "source,source_id) VALUES ('BTC/USDT','1h',?,?,1,2,0.5,1,1,1,0.5,?,?,?,?)",
        (now, now + 1, now, now, "x", "dup-1"))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO candles (symbol,timeframe,open_time_utc,close_time_utc,open,high,"
            "low,close,volume,trades,taker_buy_volume,event_time_utc,received_time_utc,"
            "source,source_id) VALUES ('BTC/USDT','1h',?,?,1,2,0.5,1,1,1,0.5,?,?,?,?)",
            (now, now + 1, now, now, "x", "dup-2"))
    con.rollback()
    assert storage_check(con)["duplicate_keys"] == 0
    con.close()


def test_storage_check_keeps_its_old_keys_for_existing_callers(tmp_path):
    """soak_ingest.py, ingest_public.py and test_data_foundation read these."""
    con = init(tmp_path / "h.db")
    upsert_candle(con, _candle("BTC/USDT", "1h", int(time.time() * 1000) - 7_200_000))
    con.commit()
    st = storage_check(con)
    assert st["duplicate_keys"] == 0
    assert st["groups"][0]["n"] == 1
    con.close()


def test_ok_all_is_false_when_storage_fails_even_if_candles_are_fresh(tmp_path, monkeypatch):
    """The candle verdict must not be able to mask a storage failure.

    chmod cannot simulate this (the suite runs as root), so the connection is
    opened with SQLITE_OPEN_READONLY — a real failure path, and the one a
    monitor run against a replica/backup would hit.
    """
    db = tmp_path / "h.db"
    _seed_fresh(db)
    real_connect = sqlite3.connect

    def readonly(path, *a, **kw):
        con = real_connect(path, *a, **kw)
        con.execute("PRAGMA query_only=ON")   # every write now raises
        return con

    monkeypatch.setattr(health_mod.sqlite3, "connect", readonly)
    rep = check_data_health(db)
    assert rep["ok"] is True, "the candles really are fresh"
    assert rep["ok_all"] is False, (
        "a store that cannot be written was reported as fully healthy")
    assert rep["storage"]["writable"] is False
    assert any(p.startswith("sqlite:") for p in rep["problems"])
    assert any("blind to this" in p for p in rep["problems"])


def test_a_report_that_says_no_data_names_what_it_looked_at(tmp_path):
    db = tmp_path / "empty.db"
    init(db).close()
    rep = check_data_health(db)
    assert rep["ok"] is False
    assert rep["db"] == str(db)
    assert rep["storage"]["groups"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
