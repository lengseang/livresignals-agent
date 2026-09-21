"""P1-02 tests: normalization closed-candle rule, idempotent storage, gap detection."""
from __future__ import annotations

import time

import pytest

from trading.data.normalize import detect_gaps, is_fresh, normalize_candle
from trading.data.store import init, storage_check, upsert_candle


def test_normalize_rejects_open_candle():
    ts = int(time.time() * 1000)  # current-time candle is open
    raw = [ts, 100.0, 101.0, 99.0, 100.5, 10.0]
    assert normalize_candle(raw, "BTC/USDT", "1h") is None


def test_normalize_accepts_closed_candle():
    ts = int(time.time() * 1000) - 3 * 3600_000  # 3h ago, 1h candle is closed
    raw = [ts, 100.0, 101.0, 99.0, 100.5, 10.0, None, None, 25, 4.0]
    row = normalize_candle(raw, "BTC/USDT", "1h")
    assert row is not None
    assert row["symbol"] == "BTC/USDT"
    assert row["close_time_utc"] == ts + 3600_000 - 1
    assert row["trades"] == 25


def test_storage_idempotent(tmp_path):
    con = init(tmp_path / "test.db")
    row = {
        "symbol": "ETH/USDT", "timeframe": "5m",
        "open_time_utc": 1700000000000, "close_time_utc": 1700000300000,
        "open": 2000.0, "high": 2010.0, "low": 1990.0, "close": 2005.0,
        "volume": 5.0, "trades": 10, "taker_buy_volume": 3.0,
        "event_time_utc": 1700000000000, "received_time_utc": 1700000305000,
        "source": "binance.vision", "source_id": "ETH/USDT:5m:1700000000000",
    }
    assert upsert_candle(con, row) == 1
    assert upsert_candle(con, row) == 0  # idempotent: no duplicate
    con.commit()
    check = storage_check(con)
    assert check["duplicate_keys"] == 0
    assert check["groups"][0]["n"] == 1


def test_freshness_deadline():
    now = int(time.time() * 1000)
    assert is_fresh("1h", now, now)
    assert not is_fresh("1h", now - 100 * 60_000, now)


def test_detect_gaps():
    rows = [
        {"timeframe": "5m", "open_time_utc": 1000},
        {"timeframe": "5m", "open_time_utc": 1000 + 300_000},
        {"timeframe": "5m", "open_time_utc": 1000 + 1400_000},  # 5m gap after expected 1300
    ]
    gaps = detect_gaps(rows)
    assert len(gaps) == 1
    assert gaps[0]["expected"] == 1000 + 300_000 + 300_000  # 601000
    assert gaps[0]["from"] == 1000 + 300_000
    assert gaps[0]["to"] == 1000 + 1400_000


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))