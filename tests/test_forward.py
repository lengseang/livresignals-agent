"""P-A forward-tracker tests: state persistence, wallet cap, idempotent advance."""
from __future__ import annotations

import json

import pytest

from trading.allocation import forward


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
    monkeypatch.setattr(forward, "STATE_FILE", tmp_path / "state.json")
    st = forward.load_state()
    # Tiny wallet + many entries -> should exhaust and stop recording
    st["usdt_started"] = 10.0
    # Simulate advance against the real DB (may be empty in test env -> processed small)
    res = forward.advance(st)
    assert res["processed"] >= 0
    # Every recorded entry must have amount > 0
    for e in st["entries"]:
        assert e["amount"] > 0
    # Cash never exceeds wallet
    assert st["cash_invested"] <= st["usdt_started"] + 1e-9


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