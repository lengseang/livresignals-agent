"""P1-03 tests: data health monitor semantics."""
from __future__ import annotations

import time

import pytest

from trading.data.health import check_data_health
from trading.data.store import init, upsert_candle


def _candle(symbol: str, tf: str, open_ms: int, interval_ms: int, close: float) -> dict:
    return {
        "symbol": symbol, "timeframe": tf,
        "open_time_utc": open_ms, "close_time_utc": open_ms + interval_ms - 1,
        "open": close - 1, "high": close + 1, "low": close - 2, "close": close,
        "volume": 1.0, "trades": 1, "taker_buy_volume": 0.5,
        "event_time_utc": open_ms, "received_time_utc": open_ms + interval_ms,
        "source": "test", "source_id": f"{symbol}:{tf}:{open_ms}",
    }


def test_health_ok_for_recent_closed_candles(tmp_path):
    db = tmp_path / "h.db"
    con = init(db)
    now = int(time.time() * 1000)
    # Seed every symbol/timeframe combo with a freshly-closed candle.
    interval_ms = {"5m": 300_000, "15m": 900_000, "30m": 1800_000, "1h": 3600_000,
                   "4h": 14400_000, "1d": 86400_000}
    for symbol in ("BTC/USDT", "ETH/USDT", "SOL/USDT"):
        for tf, iv in interval_ms.items():
            open_ms = now - 2 * iv  # closed 1 interval ago -> fresh
            upsert_candle(con, _candle(symbol, tf, open_ms, iv, 100.0))
    con.commit()
    con.close()

    report = check_data_health(db)
    assert report["ok"] is True
    assert report["detail"]["BTC/USDT:1h"]["fresh"] is True


def test_health_flags_stale(tmp_path):
    db = tmp_path / "h.db"
    con = init(db)
    now = int(time.time() * 1000)
    # 1h candle closed 3 hours ago -> stale
    open_ms = now - 4 * 3600_000
    upsert_candle(con, _candle("BTC/USDT", "1h", open_ms, 3600_000, 100.0))
    con.commit()
    con.close()

    report = check_data_health(db)
    assert report["ok"] is False
    assert any("stale" in p for p in report["problems"])


def test_health_flags_no_data(tmp_path):
    db = tmp_path / "empty.db"
    init(db).close()

    report = check_data_health(db)
    assert report["ok"] is False
    assert any("no data" in p for p in report["problems"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))