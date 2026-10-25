"""CCXT fetcher for Binance public market data.

Sandbox mode uses data-api.binance.vision (public mirror, verified reachable).
Live mode is BLOCKED until Phase 6 (api.binance.com DNS-blocked on this ISP).
"""
from __future__ import annotations

import ccxt

from trading.config import PUBLIC_DATA_HOST, SYMBOLS, TESTNET_HOST, TIMEFRAMES


def make_exchange(sandbox: bool = True) -> ccxt.binance:
    ex = ccxt.binance({
        "enableRateLimit": True,
        "timeout": 20_000,
        "options": {
            "defaultType": "spot",
            "adjustForTimeDifference": True,
            "fetchMarkets": {"types": ["spot"]},
        },
    })
    if sandbox:
        # Force SPOT public API onto the reachable mirror.
        ex.urls["api"] = {
            "public": f"https://{PUBLIC_DATA_HOST}/api/v3",
            "private": f"https://{TESTNET_HOST}/api/v3",
            "sapi": f"https://{PUBLIC_DATA_HOST}/sapi/v1",
            "fapiPublic": None,
            "fapiPrivate": None,
            "dapiPublic": None,
            "dapiPrivate": None,
        }
    return ex


def fetch_closed_candles(ex, symbol: str, timeframe: str, limit: int = 200) -> list[list]:
    """Fetch raw OHLCV from CCXT. Normalization happens in normalize.py."""
    return ex.fetch_ohlcv(symbol, timeframe, limit=limit)


def fetch_order_book_snapshot(ex, symbol: str, limit: int = 20) -> dict:
    ob = ex.fetch_order_book(symbol, limit=limit)
    return {"bids": ob.get("bids", []), "asks": ob.get("asks", [])}


def fetch_recent_trades(ex, symbol: str, limit: int = 50) -> list[dict]:
    trades = ex.fetch_trades(symbol, limit=limit)
    return [
        {
            "trade_id": str(t.get("id", "")),
            "ts_utc": int(t.get("timestamp") or 0),
            "price": float(t.get("price") or 0),
            "amount": float(t.get("amount") or 0),
            "is_buyer_maker": bool(t.get("side") != "buy"),
        }
        for t in trades
    ]


def symbols():
    return SYMBOLS


def timeframes():
    return TIMEFRAMES