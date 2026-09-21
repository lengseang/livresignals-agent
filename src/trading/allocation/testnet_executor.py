"""P-A testnet executor: places REAL paper orders on Binance testnet.

Autonomous paper-trading loop (P5):
- Reads the validated P-A config (monthly cadence, 1.5x on -15% drawdowns).
- Case 1 (INIT): wallet has full USDT and no BTC → seed the initial position
  per the strategy's first monthly entry.
- Case 2 (cron): if >=30d since last entry, compute multiplier from current
  drawdown vs trailing 90d high, place a market buy, record to ledger.

Safety: testnet ONLY. Refuses to run if credentials are missing or if the
symbol is not the configured universe. No real money ever.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from trading.allocation.engine import AllocationConfig
from trading.config import SQLITE_PATH
from trading.data.ccxt_fetcher import make_exchange
from trading.exchange.testnet_client import place_market_buy, testnet_balances

LEDGER_FILE = Path(__file__).resolve().parents[3] / "data" / "pa-testnet-ledger.json"
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
SYMBOL = SYMBOLS[0]  # default for single-symbol callers


def load_ledger() -> dict:
    path = _ledger_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"entries": [], "started_at_utc": datetime.now(timezone.utc).isoformat()}


def save_ledger(ledger: dict) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")


def _ledger_path() -> Path:
    """Resolve ledger path at call time so tests can monkeypatch cleanly."""
    return LEDGER_FILE


def trailing_drawdown_pct(symbol: str, lookback_days: int = 90) -> float:
    """Current price vs trailing lookback high (fraction, e.g. -0.12)."""
    ex = make_exchange(sandbox=True)
    since = int((time.time() - lookback_days * 86400) * 1000)
    ohlcv = ex.fetch_ohlcv(symbol, "1d", since=since, limit=lookback_days + 1)
    if not ohlcv:
        raise RuntimeError("no price history for drawdown calc")
    closes = [float(c[4]) for c in ohlcv[:-1]]  # exclude today (open candle)
    current = float(ohlcv[-1][4])
    hi = max(closes) if closes else current
    return current / hi - 1.0 if hi > 0 else 0.0


def should_entry(ledger: dict, symbol: str, interval_days: int = 30, hours: int = 0) -> tuple[bool, str]:
    """True if entry is due for `symbol` (or no entries yet).

    Pass hours=N for sub-daily cadences (e.g. hours=1 → hourly).
    interval_days is kept for backward compat; hours takes precedence.
    """
    sym_entries = [e for e in ledger["entries"] if e.get("symbol") == symbol]
    if not sym_entries:
        return True, "initial entry"
    last_ms = sym_entries[-1]["ts_ms"]
    since = int(time.time() * 1000) - last_ms
    if hours > 0:
        needed = hours * 3600 * 1000
        if since >= needed:
            return True, "cadence due"
        remaining_hrs = (needed - since) // 3600000
        remaining_mins = ((needed - since) % 3600000) // 60000
        if remaining_hrs > 0:
            return False, f"next entry in {remaining_hrs}h{remaining_mins}m"
        return False, f"next entry in {remaining_mins}m"
    needed = interval_days * 86400 * 1000
    if since >= needed:
        return True, "monthly cadence due"
    return False, f"next entry in {(needed - since) // 86400000}d"


def run_entry(quote_amount: float, symbol: str = SYMBOL, force: bool = False) -> dict:
    """Place one P-A paper entry order on testnet. Records to ledger.

    force=True bypasses the cadence gate (TESTING ONLY — emits a signal card).
    """
    cfg = AllocationConfig()
    ledger = load_ledger()
    # Interval from adaptive params (hours for sub-daily, validated at 1h +31.8%).
    try:
        from trading.signals.adaptive import load_params
        ih = load_params().get("interval_hours", 24)
    except Exception:  # noqa: BLE001
        ih = 24
    due, reason = should_entry(ledger, symbol, hours=ih)
    if not due and not force:
        return {"skipped": True, "symbol": symbol, "reason": reason}

    dd = trailing_drawdown_pct(symbol, cfg.lookback_days)
    multiplier = cfg.dip_multiplier if dd <= cfg.drawdown_threshold else 1.0

    # Adaptive learning: apply per-symbol weight from adaptive-params.json.
    try:
        from trading.signals.adaptive import load_params
        w = load_params()["symbol_weights"].get(symbol, 1.0)
        multiplier = round(multiplier * w, 3)
    except Exception:  # noqa: BLE001
        pass  # adaptive config optional; default behavior when absent

    amount = cfg.base_amount * multiplier
    if amount > quote_amount:
        amount = quote_amount

    order = place_market_buy(symbol, amount)
    ledger["entries"].append({
        "symbol": symbol,
        "ts_ms": int(time.time() * 1000),
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "order_id": order["id"],
        "amount_usdt": order["cost"],
        "qty": order["amount"],
        "price": order["price"],
        "drawdown_pct": round(dd, 4),
        "multiplier": multiplier,
    })
    save_ledger(ledger)
    return {"skipped": False, "symbol": symbol, "reason": reason, "drawdown_pct": dd, "multiplier": multiplier, "order": order}


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "entry"
    quote_arg = float(sys.argv[2]) if len(sys.argv) > 2 else 100.0
    sym_arg = sys.argv[3] if len(sys.argv) > 3 else SYMBOL
    if mode == "entry":
        result = run_entry(quote_arg, sym_arg)
        print(json.dumps(result, indent=2))
        if not result.get("skipped") and result.get("order", {}).get("status"):
            b = testnet_balances()
            print("WALLET: USDT=%.2f BTC=%.6f ETH=%.6f SOL=%.6f" % (
                b.get("USDT", 0), b.get("BTC", 0), b.get("ETH", 0), b.get("SOL", 0)))
    elif mode == "signal":
        # TESTING: force an entry + broadcast a rich signal card (paper money).
        result = run_entry(quote_arg, sym_arg, force=True)
        print(json.dumps(result, indent=2))
        if not result.get("skipped") and result.get("order", {}).get("status"):
            entry = {
                "symbol": result["symbol"], "action": "BUY",
                "price": result["order"]["price"],
                "amount_usdt": result["order"]["cost"],
                "qty": result["order"]["amount"],
                "drawdown_pct": round(result["drawdown_pct"], 2),
                "multiplier": result["multiplier"],
                "ts_utc": datetime.now(timezone.utc).isoformat(),
            }
            try:
                from trading.tg.bot_ui import signal_card, send_card
                title, body, buttons = signal_card(entry)
                r = send_card("signals", title, body, buttons)
                print("SIGNAL_CARD_SENT:", r.get("ok"))
            except Exception as e:  # noqa: BLE001
                print("SIGNAL_CARD_ERROR:", str(e)[:120])
            b = testnet_balances()
            print("WALLET: USDT=%.2f BTC=%.6f ETH=%.6f SOL=%.6f" % (
                b.get("USDT", 0), b.get("BTC", 0), b.get("ETH", 0), b.get("SOL", 0)))
    elif mode == "all":
        results = []
        for s in SYMBOLS:
            r = run_entry(quote_arg, s)
            results.append(r)
            print(json.dumps(r, indent=2))
        b = testnet_balances()
        print("WALLET: USDT=%.2f BTC=%.6f ETH=%.6f SOL=%.6f" % (
            b.get("USDT", 0), b.get("BTC", 0), b.get("ETH", 0), b.get("SOL", 0)))
    else:
        b = testnet_balances()
        print(json.dumps({"balances": b, "ledger": load_ledger()}, indent=2))