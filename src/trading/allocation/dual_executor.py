"""P-A dual-track executor — paper + live money in parallel (spec §3).

Every strategy entry goes to the PAPER track (testnet, control group).
Entries that clear the confidence bar ALSO go to the LIVE track (real
money, tiny size) IF the live wallet is configured. Any exception during
a live order is logged to the bugs log and THAT entry still executes on
paper — live never silently skips, and paper never stops.

Ledgers (separate, never mixed):
  data/pa-testnet-ledger.json  — paper track (existing)
  data/pa-live-ledger.json     — live track (new)
  data/bugs-log.md             — append-only bug/incident log
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from trading.allocation.confidence import (
    live_enabled,
    live_size,
    load_config,
    should_route_live,
)
from trading.allocation.engine import AllocationConfig
from trading.allocation.testnet_executor import (
    load_ledger as load_paper_ledger,
    run_entry as run_paper_entry,
    save_ledger as save_paper_ledger,
)
from trading.config import SQLITE_PATH
from trading.data.ccxt_fetcher import make_exchange

LIVE_LEDGER_FILE = Path(__file__).resolve().parents[3] / "data" / "pa-live-ledger.json"
BUGS_LOG_FILE = Path(__file__).resolve().parents[3] / "data" / "bugs-log.md"
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]


# ---------------------------------------------------------------------------
# Live ledger (tiny real-money track)
# ---------------------------------------------------------------------------
def load_live_ledger() -> dict:
    if LIVE_LEDGER_FILE.exists():
        try:
            return json.loads(LIVE_LEDGER_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"entries": [], "started_at_utc": datetime.now(timezone.utc).isoformat()}


def save_live_ledger(ledger: dict) -> None:
    LIVE_LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    LIVE_LEDGER_FILE.write_text(json.dumps(ledger, indent=2), encoding="utf-8")


def live_should_entry(ledger: dict, symbol: str, interval_days: int = 30) -> tuple[bool, str]:
    """Same cadence rule as paper, evaluated against the LIVE ledger.

    Each track has its own cadence clock: the live track starts fresh when
    the wallet is added, so it does not inherit paper history.
    """
    sym_entries = [e for e in ledger["entries"] if e.get("symbol") == symbol]
    if not sym_entries:
        return True, "initial live entry"
    last_ms = sym_entries[-1]["ts_ms"]
    since = int(time.time() * 1000) - last_ms
    if since >= interval_days * 86400 * 1000:
        return True, "monthly cadence due (live)"
    return False, f"next live entry in {(interval_days*86400*1000 - since) // 86400000}d"


# ---------------------------------------------------------------------------
# Bugs log (record bugs on the way)
# ---------------------------------------------------------------------------
def log_bug(component: str, severity: str, description: str, impact: str = "") -> None:
    """Append a row to data/bugs-log.md. Never raises (logging must not break trading)."""
    try:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        row = f"| {ts} | {component} | {severity} | {description} | {impact} | open |\n"
        header = (
            "| ts_utc | component | severity | description | impact | status |\n"
            "|---|---|---|---|---|---|\n"
        )
        if not BUGS_LOG_FILE.exists():
            BUGS_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            BUGS_LOG_FILE.write_text(
                "# Trading Bugs Log\n\nAuto-written by dual_executor + manual observations.\n\n" + header,
                encoding="utf-8",
            )
        else:
            content = BUGS_LOG_FILE.read_text(encoding="utf-8")
            if header.strip() not in content:
                content += "\n" + header
                BUGS_LOG_FILE.write_text(content, encoding="utf-8")
        # Always append the row (fresh file = header written above, then row).
        with BUGS_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(row)
    except Exception:  # noqa: BLE001
        pass  # logging failure must never stop an order


# ---------------------------------------------------------------------------
# Live order (with safety rails)
# ---------------------------------------------------------------------------
def place_live_order(symbol: str, quote_amount: float) -> dict:
    """Place ONE real order. Raises if live is not properly configured."""
    from trading.exchange.live_client import place_market_buy  # lazy import

    order = place_market_buy(symbol, quote_amount)
    ledger = load_live_ledger()
    ledger["entries"].append({
        "symbol": symbol,
        "ts_ms": int(time.time() * 1000),
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "order_id": order["id"],
        "amount_usdt": order["cost"],
        "qty": order["amount"],
        "price": order["price"],
        "track": "live",
    })
    save_live_ledger(ledger)
    return order


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
def run_all(quote_amount: float = 100.0, force: bool = False) -> dict:
    """Run the dual-track loop for all symbols. Returns a summary dict.

    force=True bypasses cadence gates (TESTING ONLY — emits signal cards).
    """
    cfg = load_config()
    conf_cfg = AllocationConfig()
    paper = load_paper_ledger()
    live = load_live_ledger()
    live_on = live_enabled()
    results = []

    for sym in SYMBOLS:
        try:
            dd = _drawdown(sym)
            detectors = _detectors(sym)
            confidence = _confidence(dd, detectors)
            paper_due, paper_reason = _paper_due(paper, sym, conf_cfg, force)

            row = {
                "symbol": sym,
                "drawdown_pct": dd,
                "confidence": confidence,
                "paper_due": paper_due,
                "paper_reason": paper_reason,
                "route": "paper",
                "live_order": None,
                "error": None,
            }

            # Paper track: ALWAYS takes the entry when due.
            if paper_due:
                try:
                    pr = run_paper_entry(quote_amount, sym, force=force)
                    row["paper_order"] = pr.get("order", {}).get("id") if not pr.get("skipped") else None
                except Exception as e:  # noqa: BLE001
                    row["error"] = f"paper: {e}"
                    log_bug("dual_executor", "HIGH", f"paper entry failed {sym}: {e}")

            # Live track: only if enabled, due on live cadence, and confidence clears.
            live_due, live_reason = live_should_entry(live, sym, conf_cfg.interval_days)
            row["live_due"] = live_due
            row["live_reason"] = live_reason

            if live_on and live_due and should_route_live(confidence, cfg):
                size = live_size(quote_amount, cfg)
                try:
                    order = place_live_order(sym, size)
                    row["route"] = "live"
                    row["live_order"] = order["id"]
                except Exception as e:  # noqa: BLE001
                    row["error"] = f"live: {e}"
                    log_bug(
                        "dual_executor",
                        "CRITICAL",
                        f"live order failed {sym} ({size} USDT): {e}",
                        "paper entry unaffected; live skipped this cycle",
                    )
            elif live_on and live_due:
                row["live_reason"] = f"confidence {confidence:.2f} < threshold"

            results.append(row)
        except Exception as e:  # noqa: BLE001 — per-symbol isolation
            results.append({"symbol": sym, "error": f"router: {e}"})
            log_bug("dual_executor", "HIGH", f"router error {sym}: {e}")

    return {
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "live_enabled": live_on,
        "results": results,
    }


def _drawdown(symbol: str) -> float:
    from trading.allocation.testnet_executor import trailing_drawdown_pct

    return round(trailing_drawdown_pct(symbol), 4)


def _detectors(symbol: str) -> list[str]:
    """Currently-firing signal types for a symbol (empty on any failure)."""
    try:
        from trading.signals.multi_signal import market_features, detect

        feats = market_features(symbol)
        return [s["signal_type"] for s in detect(symbol, feats)]
    except Exception:  # noqa: BLE001
        return []


def _confidence(dd: float, detectors: list[str]) -> float:
    from trading.allocation.confidence import compute_confidence
    from trading.signals.regime import classify_regime

    try:
        regime = classify_regime([])  # best-effort; UNKNOWN if empty
    except Exception:  # noqa: BLE001
        regime = "UNKNOWN"
    return compute_confidence(dd, detectors, regime)


def _paper_due(paper: dict, sym: str, cfg: AllocationConfig, force: bool) -> tuple[bool, str]:
    from trading.allocation.testnet_executor import should_entry

    if force:
        return True, "forced (testing)"
    return should_entry(paper, sym, cfg.interval_days)


if __name__ == "__main__":
    import sys

    force = "force" in sys.argv[1:]
    print(json.dumps(run_all(100.0, force=force), indent=2))
