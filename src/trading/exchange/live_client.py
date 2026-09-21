"""Live Binance client — REAL money spot execution (Phase 6, dual-track LIVE leg).

Credentials are read from config/live.env (0600, never committed).
This is the ONLY module allowed to place REAL orders, and ONLY when:
  1. config/live.env exists with valid withdrawal-disabled keys
  2. TRADING_ENV=live (or dual-track live_enabled=true)

Safety: the module refuses to construct the client if credentials are
missing/short, mirroring testnet_client.py. No withdrawal path exists in
code — the API key itself must be withdrawal-disabled (policy, checked
by ops on first setup).
"""
from __future__ import annotations

import os
from pathlib import Path

import ccxt

ENV_FILE = Path(__file__).resolve().parents[3] / "config" / "live.env"


def load_live_credentials() -> dict[str, str]:
    """Read config/live.env (never echo secrets). Returns {} if absent."""
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


def make_live_exchange() -> ccxt.binance:
    """Create CCXT binance pointed at the real API with live credentials.

    Raises ValueError if credentials missing/invalid — callers must treat
    this as "live NOT available", NEVER as permission to paper-fallback
    silently (routing is decided before the order is placed).
    """
    creds = load_live_credentials()
    api_key = creds.get("LIVE_API_KEY", "")
    secret = creds.get("LIVE_SECRET_KEY", "")
    if not api_key or not secret or len(api_key) < 30 or len(secret) < 30:
        raise ValueError("live credentials missing/invalid in config/live.env")

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
    # ISP DNS-block workaround (Cambodia): api.binance.com resolves to a bogus
    # private IP via the ISP resolver. Patch socket.getaddrinfo so the HTTP
    # client connects to the CloudFront direct IP while keeping the correct
    # Host header + TLS SNI (api.binance.com). Public, non-region-locked.
    # IDEMPOTENT: guard with a sentinel so repeated calls (the watchdog calls
    # this every 30s) do NOT stack nested patches and blow the recursion limit.
    try:
        import socket as _socket
        _PATCH_SENTINEL = "_binance_dns_patched"
        if not getattr(_socket, _PATCH_SENTINEL, False):
            _real_getaddrinfo = _socket.getaddrinfo
            _API_IP = "13.249.239.121"  # api.binance.com CloudFront (via Google DNS)
            def _patched_getaddrinfo(host, *args, **kwargs):
                if host == "api.binance.com":
                    host = _API_IP
                return _real_getaddrinfo(host, *args, **kwargs)
            _socket.getaddrinfo = _patched_getaddrinfo
            _socket._binance_dns_patched = True
    except Exception:  # noqa: BLE001
        pass  # fall back to normal resolution if the patch fails
    # Sync clock so signed requests pass the -1021 check (same as testnet).
    try:
        ex.load_time_difference()
    except Exception:  # noqa: BLE001 — best effort; caller may see skew error
        pass
    return ex


def live_balances() -> dict[str, float]:
    """Fetch live spot balances (non-zero only). Never logs secrets."""
    ex = make_live_exchange()
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
    """Place a REAL spot MARKET BUY (quote currency, e.g. USDT).

    Mirrors testnet_client.place_market_buy — same raw-endpoint strategy
    (privatePostOrder with quoteOrderQty, seeded markets from exchangeInfo)
    so the live path behaves identically to the validated paper path.
    """
    ex = make_live_exchange()
    _seed_markets_from_exchange_info(ex)
    market = ex.market(symbol)
    ticker = ex.publicGetTickerPrice({"symbol": market["id"]})
    price = float(ticker["price"])
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


def place_market_sell(symbol: str, base_amount: float) -> dict:
    """Place a REAL spot MARKET SELL (base currency, e.g. BTC).

    Mirrors place_market_buy but SELL side, sized in BASE asset quantity.
    """
    ex = make_live_exchange()
    _seed_markets_from_exchange_info(ex)
    market = ex.market(symbol)
    resp = ex.privatePostOrder({
        "symbol": market["id"],
        "side": "SELL",
        "type": "MARKET",
        "quantity": f"{base_amount:.8f}",
        "recvWindow": 10_000,
        "newOrderRespType": "FULL",
    })
    fills = resp.get("fills", [])
    total_qty = sum(float(f.get("qty", 0)) for f in fills)
    total_cost = sum(float(f.get("price", 0)) * float(f.get("qty", 0)) for f in fills)
    return {
        "id": resp.get("orderId"),
        "clientOrderId": resp.get("clientOrderId"),
        "symbol": symbol,
        "side": "sell",
        "type": "market",
        "amount": total_qty,
        "price": total_cost / total_qty if total_qty else 0.0,
        "cost": total_cost,
        "status": resp.get("status"),
        "timestamp": resp.get("transactTime"),
    }


def _seed_markets_from_exchange_info(ex: ccxt.binance) -> None:
    """Populate ex.markets from PUBLIC /api/v3/exchangeInfo (live-safe)."""
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
        b = live_balances()
        print("LIVE BALANCES:", {k: round(v, 6) for k, v in b.items()})
    except ValueError as e:
        print("CREDENTIALS:", e)
    except Exception as e:  # noqa: BLE001
        print("LIVE ERROR:", str(e)[:200])
