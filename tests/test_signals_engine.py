"""Signal engine + accuracy tests."""
from __future__ import annotations

import pytest

from trading.signals import accuracy as acc
from trading.signals import signal_engine as eng


def _sig(symbol="BTC/USDT", bias="BULLISH", price=100.0, ts_ms=1, horizon_h=24, outcome=None):
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        "id": f"{symbol}-{ts_ms}", "ts_utc": f"{today}T00:00:00Z", "ts_ms": ts_ms,
        "symbol": symbol, "action": "BUY" if bias == "BULLISH" else "WATCH",
        "bias": bias, "price": price, "drawdown_pct": -0.19, "regime": "RANGING",
        "horizon_h": horizon_h, "predicted": "up" if bias == "BULLISH" else "down",
        "reason": "test", "outcome": outcome,
    }


def test_resolve_outcomes_correct(tmp_path, monkeypatch):
    ledger = {"signals": [
        _sig("BTC/USDT", "BULLISH", 100.0, ts_ms=1, outcome=None),
        _sig("ETH/USDT", "BEARISH", 100.0, ts_ms=2, outcome=None),
    ]}
    monkeypatch.setattr(eng, "SIGNAL_LEDGER", tmp_path / "ledger.json")
    eng._save_ledger(ledger)
    # Force horizon elapsed: ts_ms=1 is ancient; prices: BTC up, ETH down
    import time
    now = int(time.time() * 1000)
    ledger["signals"][0]["ts_ms"] = now - 48 * 3600 * 1000
    ledger["signals"][1]["ts_ms"] = now - 48 * 3600 * 1000
    eng._save_ledger(ledger)
    n = eng.resolve_outcomes(prices={"BTC/USDT": 110.0, "ETH/USDT": 90.0})
    assert n == 2
    l2 = eng._load_ledger()
    assert l2["signals"][0]["outcome"]["correct"] is True   # BTC up, predicted up
    assert l2["signals"][1]["outcome"]["correct"] is True   # ETH down, predicted down


def test_accuracy_stats_shape(tmp_path, monkeypatch):
    ledger = {"signals": [
        _sig("BTC/USDT", "BULLISH", 100.0, ts_ms=1, outcome={"correct": True, "actual_pct": 5.0}),
        _sig("ETH/USDT", "BEARISH", 100.0, ts_ms=2, outcome={"correct": False, "actual_pct": 3.0}),
        _sig("SOL/USDT", "BULLISH", 100.0, ts_ms=3, outcome={"correct": True, "actual_pct": 1.0}),
    ]}
    monkeypatch.setattr(eng, "SIGNAL_LEDGER", tmp_path / "ledger.json")
    eng._save_ledger(ledger)
    stats = acc.accuracy_stats()
    assert stats["resolved"] == 3
    assert stats["accuracy_pct"] == pytest.approx(66.7, abs=0.1)
    by_sym = {b["bucket"]: b["accuracy"] for b in stats["per_symbol"]}
    assert by_sym["BTC/USDT"] == 100.0
    assert by_sym["ETH/USDT"] == 0.0


def test_daily_summary_counts(tmp_path, monkeypatch):
    ledger = {"signals": [
        _sig("BTC/USDT", "BULLISH", ts_ms=1),
        _sig("ETH/USDT", "BEARISH", ts_ms=2),
    ]}
    monkeypatch.setattr(eng, "SIGNAL_LEDGER", tmp_path / "ledger.json")
    eng._save_ledger(ledger)
    s = acc.daily_summary()
    from datetime import datetime, timezone
    assert s["date"] == datetime.now(timezone.utc).date().isoformat()
    assert s["signals_today"] == 2
    assert s["by_action"]["BUY"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))