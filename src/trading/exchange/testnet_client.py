"""Testnet Binance client — paper-trading execution on testnet.binance.vision.

Credentials are read from config/testnet.env (0600, never committed).
This is the ONLY module allowed to place orders, and only in sandbox mode.
Real-money trading requires a separate live client + user wallet config.
"""
from __future__ import annotations

import os
from pathlib import Path

import ccxt

ENV_FILE = Path(__file__).resolve().parents[3] / "config" / "testnet.env"


def load_testnet_credentials() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    creds = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        creds[k.strip()] = v.strip()
    return creds


def make_testnet_exchange() -> ccxt.binance:
    """Create a CCXT binance instance pointed at testnet with credentials.

    Auto-syncs the local clock to the testnet server (avoids -1021 skew).
    Raises ValueError if credentials missing (caller must handle gracefully).
    """
    creds = load_testnet_credentials()
    api_key = creds.get("TESTNET_API_KEY", "")
    secret = creds.get("TESTNET_SECRET_KEY", "")
    if not api_key or not secret or len(api_key) < 30 or len(secret) < 30:
        raise ValueError("testnet credentials missing/invalid in config/testnet.env")

    ex = ccxt.binance({
        "apiKey": api_key,
        "secret": secret,
        "enableRateLimit": True,
        "timeout": 20_000,
        "options": {
            "defaultType": "spot",
            "adjustForTimeDifference": True,
            "fetchMarkets": {"types": ["spot"]},
        },
    })
    ex.urls["api"] = {
        "public": "https://testnet.binance.vision/api/v3",
        "private": "https://testnet.binance.vision/api/v3",
        "sapi": "https://testnet.binance.vision/sapi/v1",
        "fapiPublic": None,
        "fapiPrivate": None,
        "dapiPublic": None,
        "dapiPrivate": None,
    }
    # Sync clock to testnet server so signed requests pass the -1021 check.
    try:
        ex.load_time_difference()
    except Exception:  # noqa: BLE001 — best effort; caller may see skew error
        pass
    return ex


def testnet_balances() -> dict[str, float]:
    """Fetch testnet spot balances (non-zero only). Never logs secrets.

    Uses /api/v3/account directly — CCXT fetch_balance routes to sapi which
    testnet does not serve (404).
    """
    ex = make_testnet_exchange()
    acct = ex.privateGetAccount({"recvWindow": 10_000})
    balances = acct.get("balances", [])
    out = {}
    for b in balances:
        free = float(b.get("free", 0) or 0)
        locked = float(b.get("locked", 0) or 0)
        if free + locked > 0:
            out[b.get("asset", "?")] = free + locked
    return out


def place_market_buy(symbol: str, quote_amount: float) -> dict:
    """Place a testnet spot MARKET BUY (quote currency, e.g. USDT).

    Paper-money only. Returns order details. Raises on failure.
    NOTE: load_markets() hits a sapi endpoint testnet does not serve, so we
    seed the market map directly for the three symbols we trade.
    """
    ex = make_testnet_exchange()
    _seed_markets_from_exchange_info(ex)
    market = ex.market(symbol)
    # Fetch price via raw public endpoint (avoids CCXT market schema issues).
    ticker = ex.publicGetTickerPrice({"symbol": market["id"]})
    price = float(ticker["price"])
    # Place order via raw private endpoint with quoteOrderQty (spot v3
    # supports quote-amount market orders; avoids CCXT schema assumptions).
    resp = ex.privatePostOrder({
        "symbol": market["id"],
        "side": "BUY",
        "type": "MARKET",
        "quoteOrderQty": str(round(quote_amount, 2)),
        "recvWindow": 10_000,
        "newOrderRespType": "FULL",
    })
    fills = resp.get("fills", [])
    total_cost = sum(float(f.get("price", 0)) * float(f.get("qty", 0)) for f in fills)
    total_qty = sum(float(f.get("qty", 0)) for f in fills)
    return {
        "id": resp.get("orderId"),
        "clientOrderId": resp.get("clientOrderId"),
        "symbol": symbol,
        "side": "buy",
        "type": "market",
        "amount": total_qty,
        "price": total_cost / total_qty if total_qty else price,
        "cost": total_cost,
        "status": resp.get("status"),
        "timestamp": resp.get("transactTime"),
    }


def _seed_markets_from_exchange_info(ex: ccxt.binance) -> None:
    """Populate ex.markets from PUBLIC /api/v3/exchangeInfo (testnet-safe)."""
    if ex.markets is None:
        ex.markets = {}
    if ex.markets_by_id is None:
        ex.markets_by_id = {}
    info = ex.publicGetExchangeInfo()
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT":
            continue
        sym = f"{s['baseAsset']}/{s['quoteAsset']}"
        ex.markets[sym] = {
            "id": s["symbol"],
            "symbol": sym,
            "base": s["baseAsset"],
            "quote": s["quoteAsset"],
            "type": "spot",
            "active": True,
            "precision": {"amount": s.get("baseAssetPrecision", 8), "price": s.get("quotePrecision", 8)},
            "limits": {"amount": {"min": 0.0}, "price": {"min": 0.0}},
        }
        ex.markets_by_id[s["symbol"]] = ex.markets[sym]


if __name__ == "__main__":
    try:
        b = testnet_balances()
        print("TESTNET BALANCES:", {k: round(v, 6) for k, v in b.items()})
    except ValueError as e:
        print("CREDENTIALS:", e)
    except Exception as e:  # noqa: BLE001
        print("TESTNET ERROR:", str(e)[:200])