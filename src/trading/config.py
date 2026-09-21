"""Trading project configuration.

SYMBOLS, TIMEFRAMES, and endpoint strategy for P1 data foundation.
All times UTC. Source-of-truth per docs/data-contract.md.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # Z:\Hermes\home\trading-agent


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# Environment: sandbox (paper) or live. Live trades are BLOCKED until Phase 6.
ENVIRONMENT = os.getenv("TRADING_ENV", "sandbox")

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
TIMEFRAMES = ["5m", "30m", "1h", "4h", "1d"]

# Endpoint strategy per data-contract.md §2 (measured 2026-08-11).
# api.binance.com is DNS-blocked on this ISP; binance.vision mirrors work.
CCXT_OPTIONS = {
    "enableRateLimit": True,
    "timeout": 20_000,
}

# data-api.binance.vision serves public market data; testnet serves paper.
PUBLIC_DATA_HOST = os.getenv("PUBLIC_DATA_HOST", "data-api.binance.vision")
TESTNET_HOST = os.getenv("TESTNET_HOST", "testnet.binance.vision")
LIVE_HOST = os.getenv("LIVE_HOST", "api.binance.com")

# Freshness deadlines (seconds) — provisional from data-contract §5,
# to be confirmed by P1-OD-01 latency measurements.
FRESHNESS_DEADLINES = {
    "5m": 5 * 60 + 30,
    "30m": 30 * 60 + 30,
    "1h": 60 * 60 + 30,
    "4h": 4 * 60 * 60 + 30,
    "1d": 24 * 60 * 60 + 60,
}

SQLITE_PATH = Path(os.getenv("TRADING_DB", str(PROJECT_ROOT / "data" / "trading.db")))
REPORTS_DIR = PROJECT_ROOT / "reports"

# Allow running data ingestion against the mirror in sandbox.
SANDBOX_OK = _bool("SANDBOX_OK", True)


def public_host_for(ccxt_exchange: str) -> str | None:
    """Return the binance.vision host to use for public data in sandbox."""
    if ccxt_exchange != "binance":
        return None
    return PUBLIC_DATA_HOST