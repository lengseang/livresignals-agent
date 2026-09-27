"""P-A forward-tracker tests: state persistence, wallet cap, idempotent advance."""
from __future__ import annotations

import json

import pytest

from trading.allocation import forward
from trading.allocation.engine import AllocationConfig


def test_load_state_default(tmp_path, monkeypatch):
    monkeypatch.setattr(forward, "STATE_FILE", tmp_path / "state.json")
    st = forward.load_state()
    assert st["last_processed_idx"] == -1
    assert st["btc"] == 0.0
    assert st["usdt_started"] == 10_000.0


def test_save_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(forward, "STATE_FILE", tmp_path / "state.json")
    st = forward.load_state()
    st["btc"] = 0.5
    st["last_processed_idx"] = 100
    forward.save_state(st)
    st2 = forward.load_state()
    assert st2["btc"] == 0.5
    assert st2["last_processed_idx"] == 100


def test_wallet_cap_no_dead_entries(tmp_path, monkeypatch):
    # Seeded DB, not data/trading.db: against the real DB this test failed to
    # open the file off the VPS, and on the VPS an empty table made every
    # assertion pass vacuously.
    _seed_db(tmp_path, monkeypatch)
    monkeypatch.setattr(forward, "STATE_FILE", tmp_path / "state.json")
    st = forward.load_state()
    st["usdt_started"] = 10.0
    cfg = AllocationConfig(base_amount=4.0, interval_days=1, lookback_days=5)
    res = forward.advance(st, cfg)
    assert res["processed"] == 200
    # $10 wallet at $4 a buy: 4 + 4 + the 2 left, then nothing more is recorded.
    assert [e["amount"] for e in st["entries"]] == [4.0, 4.0, 2.0]
    assert st["cash_invested"] == pytest.approx(st["usdt_started"])


def _seed_db(tmp_path, monkeypatch):
    """Create a temp DB with a candles table + a few rows, patch SQLITE_PATH."""
    import sqlite3
    db = tmp_path / "t.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE candles (symbol TEXT, timeframe TEXT, open_time_utc INTEGER, close REAL)")
    for i in range(200):
        con.execute("INSERT INTO candles VALUES ('BTC/USDT','1d',?,100.0+?)", (i * 86400_000, i * 0.1))
    con.commit()
    con.close()
    monkeypatch.setattr(forward, "SQLITE_PATH", db)
    return db


def test_advance_idempotent(tmp_path, monkeypatch):
    _seed_db(tmp_path, monkeypatch)
    monkeypatch.setattr(forward, "STATE_FILE", tmp_path / "state.json")
    st = forward.load_state()
    forward.advance(st)
    first_processed = st["last_processed_idx"]
    n1 = len(st["entries"])
    # Re-run: no new candles since -> nothing more to process
    forward.advance(st)
    assert st["last_processed_idx"] == first_processed
    assert len(st["entries"]) == n1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))