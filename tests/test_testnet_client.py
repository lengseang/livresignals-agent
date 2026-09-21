"""P5 / testnet-client tests: credentials load, balances, paper order placement.

These hit the live Binance testnet (demo money) — safe, no real funds.
Skipped automatically if credentials are not configured.
"""
from __future__ import annotations

import pytest

from trading.exchange import testnet_client


def _has_creds() -> bool:
    creds = testnet_client.load_testnet_credentials()
    return bool(creds.get("TESTNET_API_KEY") and creds.get("TESTNET_SECRET_KEY"))


@pytest.mark.skipif(not _has_creds(), reason="testnet credentials not configured")
def test_credentials_load_and_len():
    creds = testnet_client.load_testnet_credentials()
    assert len(creds["TESTNET_API_KEY"]) >= 30
    assert len(creds["TESTNET_SECRET_KEY"]) >= 30


@pytest.mark.skipif(not _has_creds(), reason="testnet credentials not configured")
def test_balances_fetch():
    b = testnet_client.testnet_balances()
    assert "USDT" in b
    assert b["USDT"] > 0


@pytest.mark.skipif(not _has_creds(), reason="testnet credentials not configured")
def test_small_market_buy_roundtrip():
    before = testnet_client.testnet_balances()
    order = testnet_client.place_market_buy("BTC/USDT", 5.0)  # tiny paper buy
    assert order["status"] == "FILLED"
    assert order["cost"] > 0
    after = testnet_client.testnet_balances()
    # USDT decreased by ~order cost; BTC increased
    assert after["USDT"] < before["USDT"]
    assert after["BTC"] > before["BTC"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))