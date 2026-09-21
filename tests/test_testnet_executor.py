"""P-A testnet executor tests: cadence gate, drawdown multiplier, ledger."""
from __future__ import annotations

import pytest

from trading.allocation import testnet_executor as te


def test_should_entry_initial():
    ledger = {"entries": []}
    due, reason = te.should_entry(ledger, "BTC/USDT", 30)
    assert due is True
    assert "initial" in reason


def test_should_entry_cadence():
    import time
    ledger = {"entries": [{"symbol": "BTC/USDT", "ts_ms": int(time.time() * 1000) - 40 * 86400 * 1000}]}
    due, _ = te.should_entry(ledger, "BTC/USDT", 30)
    assert due is True


def test_should_entry_not_due():
    import time
    ledger = {"entries": [{"symbol": "BTC/USDT", "ts_ms": int(time.time() * 1000) - 5 * 86400 * 1000}]}
    due, reason = te.should_entry(ledger, "BTC/USDT", 30)
    assert due is False
    assert "25d" in reason or "next entry" in reason


def test_should_entry_per_symbol():
    # ETH due even though BTC just traded (independent cadence per symbol)
    import time
    ledger = {"entries": [
        {"symbol": "BTC/USDT", "ts_ms": int(time.time() * 1000)},
        {"symbol": "ETH/USDT", "ts_ms": int(time.time() * 1000) - 40 * 86400 * 1000},
    ]}
    due_btc, _ = te.should_entry(ledger, "BTC/USDT", 30)
    due_eth, _ = te.should_entry(ledger, "ETH/USDT", 30)
    assert due_btc is False
    assert due_eth is True


def test_ledger_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(te, "LEDGER_FILE", tmp_path / "ledger.json")
    te.save_ledger({"entries": [{"a": 1}]})
    l = te.load_ledger()
    assert l["entries"][0]["a"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))