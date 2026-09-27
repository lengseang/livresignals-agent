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
# Freshness deadlines = bar length + ingest cadence + margin.
#
# Ingest runs at :12/:27/:42/:57 (every 15 min). A bar is only written on the
# next ingest tick AFTER it closes, so the worst-case age of a healthy series is
#   bar + 15 min  (e.g. 5m bar -> up to 20 min old and perfectly healthy).
#
# The old values were bar + 30s, which flagged every 5m series stale ~73% of the
# time on a 15-minute ingest loop. A monitor that always cries wolf is worse
# than no monitor: it hides the real failures. Do NOT tighten these without
# also tightening the ingest cadence in crontab.
INGEST_CADENCE_S = 15 * 60
FRESHNESS_MARGIN_S = 120  # +2 min slack for a slow/late ingest tick

_BAR_SECONDS = {
    "5m": 5 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
}

FRESHNESS_DEADLINES = {
    tf: bar + INGEST_CADENCE_S + FRESHNESS_MARGIN_S
    for tf, bar in _BAR_SECONDS.items()
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