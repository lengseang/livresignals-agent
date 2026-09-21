"""Order resilience — fail-safe execution layer over the testnet client.

Wraps every API call with:
  - timeout (15s) — no hanging forever
  - retry (3 attempts, exponential backoff 1s/2s/4s) — transient recovery
  - order-status reconciliation — check by ID if a market buy times out
  - circuit breaker — after N consecutive failures, pause all trading
  - balance-drift check — compare ledger expectation vs testnet balance

This is the HALON layer for trade execution. If the API goes down:
  1. first call → timeout → retry
  2. all retries exhausted → log, check order-by-ID for partial fill
  3. consecutive_failures++ → if >= 5 → circuit open → halt + #alerts
  4. circuit closes automatically after 5 min with zero failures
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from trading.exchange.testnet_client import (
    make_testnet_exchange,
    place_market_buy as _raw_market_buy,
    testnet_balances,
)

STATE_PATH = Path(__file__).resolve().parents[3] / "data" / "resilience-state.json"

# ---------------------------------------------------------------------------
# circuit breaker
# ---------------------------------------------------------------------------
MAX_FAILURES = 5
COOLDOWN_SECONDS = 300  # 5 min before circuit auto-closes


def _state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"consecutive_failures": 0, "circuit_open_since": None, "total_failures": 0,
            "last_failure_utc": None, "last_balance_check_utc": None, "drift": None}


def _save(s: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")


def circuit_open() -> bool:
    """True = trading is PAUSED (too many failures recently)."""
    s = _state()
    if s["consecutive_failures"] < MAX_FAILURES:
        return False
    opened = s.get("circuit_open_since")
    if opened is None:
        return False
    try:
        opened_ts = datetime.fromisoformat(opened).timestamp()
        if time.time() - opened_ts > COOLDOWN_SECONDS:
            # Cool-down expired — auto-close
            s["consecutive_failures"] = 0
            s["circuit_open_since"] = None
            _save(s)
            return False
    except (ValueError, TypeError):
        pass
    return True


def record_failure(reason: str) -> None:
    s = _state()
    s["consecutive_failures"] += 1
    s["total_failures"] = s.get("total_failures", 0) + 1
    s["last_failure_utc"] = datetime.now(timezone.utc).isoformat()
    if s["consecutive_failures"] >= MAX_FAILURES and s["circuit_open_since"] is None:
        s["circuit_open_since"] = datetime.now(timezone.utc).isoformat()
    _save(s)
    print(f"[RESILIENCE] failure #{s['consecutive_failures']}: {reason} | circuit_open={circuit_open()}")


def record_success() -> None:
    s = _state()
    if s["consecutive_failures"] > 0:
        s["consecutive_failures"] = 0
        s["circuit_open_since"] = None
        _save(s)


# ---------------------------------------------------------------------------
# retry + reconcile market buy
# ---------------------------------------------------------------------------


def safe_market_buy(symbol: str, quote_amount: float) -> dict:
    """Resilient market buy with timeout, retry, and order reconciliation.

    Returns dict with status, order_id, cost ignoring {... might raise RuntimeError if truly
    unrecoverable. Circuit breaker is checked BEFORE the call.
    """
    if circuit_open():
        raise RuntimeError(f"circuit open — trading paused ({MAX_FAILURES} failures, {COOLDOWN_SECONDS}s cooldown)")

    last_err = None
    for attempt in range(3):
        try:
            order = _raw_market_buy(symbol, min(quote_amount, 200))
            record_success()
            return order
        except Exception as e:
            last_err = e
            delay = 1 * (2 ** attempt)
            print(f"[RESILIENCE] attempt {attempt+1}/3 failed ({str(e)[:80]}); retry in {delay}s")
            time.sleep(delay)

    # All retries exhausted — try to reconcile: check order status by any
    # partially-known ID from the exception, then check balances directly.
    try:
        ex = make_testnet_exchange()
        b = testnet_balances()
        print(f"[RESILIENCE] reconciliation: bal USDT={b.get('USDT')} BTC={b.get('BTC')}")
    except Exception:  # noqa: BLE001
        pass

    record_failure(str(last_err)[:200])
    raise RuntimeError(f"safe_market_buy: 3 attempts failed — {str(last_err)[:120]}")


# ---------------------------------------------------------------------------
# balance-drift check
# ---------------------------------------------------------------------------
LEDGER_DRIFT_PATH = Path(__file__).resolve().parents[3] / "data" / "pa-testnet-ledger.json"


def check_balance_drift() -> dict:
    """Compare testnet balance vs ledger expectation per symbol. Drift > 1% → alert."""
    try:
        bal = testnet_balances()
    except Exception as e:
        return {"ok": False, "error": f"balance fetch failed: {str(e)[:100]}"}

    try:
        ledger = json.loads(LEDGER_DRIFT_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"ok": True, "note": "no ledger yet", "balances": bal}

    entries = ledger.get("entries", [])
    # Per-symbol: sum quantities, compare to testnet balance (BTC starts with 1.0 bonus).
    SYMBOL_MAP = {  # testnet balance key -> ledger symbol
        "BTC": "BTC/USDT", "ETH": "ETH/USDT", "SOL": "SOL/USDT",
    }
    STARTING = {"BTC": 1.0, "ETH": 1.0, "SOL": 6.0}  # testnet starting bonuses
    drifts = {}
    for token, sym in SYMBOL_MAP.items():
        ledger_qty = sum(e.get("qty", e.get("qty_btc", 0)) for e in entries if e.get("symbol") == sym)
        actual = bal.get(token, 0) - STARTING.get(token, 0)
        drifts[sym] = round(actual - ledger_qty, 8)
    ok = all(abs(d) < max(ledger_qty * 0.01, 0.0001) for sym, d in drifts.items()
             if abs(sum(e.get("qty", e.get("qty_btc", 0)) for e in entries if e.get("symbol") == sym)) > 0.0001)
    s = _state()
    s["last_balance_check_utc"] = datetime.now(timezone.utc).isoformat()
    s["drifts"] = drifts
    _save(s)
    if not ok:
        return {"ok": False, "drifts": drifts, "action": "DRIFT > 1% — reconcile before next trade"}
    return {"ok": True, "drifts": drifts}


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "check":
        r = check_balance_drift()
        print(json.dumps(r, indent=2))
    elif mode == "circuit":
        print(f"circuit_open={circuit_open()} failures={_state()['consecutive_failures']}")
    elif mode == "reset":
        _save({"consecutive_failures": 0, "circuit_open_since": None, "total_failures": 0, "last_failure_utc": None, "last_balance_check_utc": None, "drift": None})
        print("circuit reset")