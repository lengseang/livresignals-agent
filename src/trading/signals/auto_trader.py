"""Signal-triggered auto-trader: bridges signals → trades.

THE BRIDGE LAYER. Watches the signal engine's fired signals and reacts:
  - BIG signal (dip_zone / deep_dip) on a symbol → place the strategy's
    paper-market entry for that symbol IMMEDIATELY (not waiting for the
    daily loop), respecting:
      * per-symbol cadence (>= interval_days since last entry) — no spam
      * multiplier from drawdown (1.5x dip, 2.0x deep dip)
      * hard risk guards: testnet-only, capped size, universe-limited
  - Other signals (regime_shift, momentum, vol, divergence) are broadcast
    only — they inform, they don't auto-execute (no alpha claim for them).

Execution flow (called by the 4h signal-watch):
  1. scan signals (dedup state machine already handles firing-once)
  2. for each NEW BIG dip/deep signal: check cadence + guards
  3. place testnet market buy, record ledger, broadcast structured card
  4. log everything for accuracy/learning
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from trading.allocation.engine import AllocationConfig
from trading.allocation.testnet_executor import (
    SYMBOLS,
    load_ledger,
    run_entry,
    save_ledger,
    should_entry,
)
from trading.signals import multi_signal as ms

AUTO_LOG = Path(__file__).resolve().parents[3] / "data" / "auto-trade-log.json"
TRADEABLE_SIGNALS = {"dip_zone", "deep_dip"}  # only these trigger execution

# Hard risk guards — NEVER reduced (user's immutable rules).
MAX_ENTRY_USDT = 200.0        # hard cap per single entry
MAX_PORTFOLIO_USDT = 5000.0   # hard cap total invested
UNIVERSE = set(SYMBOLS)


def _load_log() -> dict:
    if AUTO_LOG.exists():
        try:
            return json.loads(AUTO_LOG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"trades": [], "skips": []}


def _save_log(log: dict) -> None:
    AUTO_LOG.parent.mkdir(parents=True, exist_ok=True)
    AUTO_LOG.write_text(json.dumps(log, indent=2), encoding="utf-8")


def _guards_ok(symbol: str) -> tuple[bool, str]:
    """Risk-guard checks before any auto-trade."""
    if symbol not in UNIVERSE:
        return False, f"{symbol} outside universe"
    ledger = load_ledger()
    invested = sum(e["amount_usdt"] for e in ledger["entries"])
    if invested + MAX_ENTRY_USDT > MAX_PORTFOLIO_USDT:
        return False, f"portfolio cap {MAX_PORTFOLIO_USDT} reached (invested {invested:.2f})"
    return True, "ok"


def _log_decision(action: str, sig: dict, reason: str) -> None:
    """Record every auto-trader decision (trade or skip) for the learning loop."""
    try:
        log = _load_log()
        entry = {
            "action": action,
            "signal": sig.get("signal_type"),
            "symbol": sig.get("symbol"),
            "reason": reason[:200],
            "ts_utc": datetime.now(timezone.utc).isoformat(),
        }
        log[("trades" if action == "trade" else "skips")].append(entry)
        _save_log(log)
    except Exception:  # noqa: BLE001
        pass  # logging must never break trading


def auto_trade_on_signal(sig: dict, quote_amount: float = 100.0) -> dict:
    """React to one fired signal: place a trade if it's a tradeable BIG one.

    Returns an action record (trade / skip / info).
    """
    stype = sig.get("signal_type")
    symbol = sig.get("symbol")

    # Non-tradeable signals: broadcast-only, no execution.
    if stype not in TRADEABLE_SIGNALS:
        return {"action": "info", "signal": stype, "symbol": symbol,
                "reason": "informational signal — no auto-execution"}

    # HARD GUARDS FIRST. The universe/portfolio guards are cheap, absolute
    # safety limits; the quality gate below is a soft, scored judgement. The
    # original order ran the scorer first, so an out-of-universe symbol was
    # scored, priced and timed before anything checked whether it was even
    # tradeable — wasted work, and the wrong precedence for a safety check.
    ok, why = _guards_ok(symbol)
    if not ok:
        _log_decision("skip", sig, why)
        return {"action": "skip", "signal": stype, "symbol": symbol, "reason": why}

    # Quality gate: score the signal, trade only if expected value is strong.
    try:
        from trading.signals.scorer import score_signal
        q = score_signal(sig)
        if q["recommended_action"] != "TRADE":
            _log_decision("skip", sig, f"quality {q['score']:.0f}: {q['recommended_action']}")
            return {"action": "skip", "signal": stype, "symbol": symbol,
                    "reason": f"quality gate: score {q['score']:.0f} ({q['recommended_action']})"}
        sig["quality_score"] = q["score"]
        sig["expected_value_pct"] = q["expected_value_pct"]
        # Real-time entry timing: blend micro-timing if samples are warm.
        # Enter only if timing is not actively chasing a spike (timing >= 50).
        try:
            from trading.signals.realtime import timing_score, combined_score
            t = timing_score(symbol)
            if t.get("ready") and t.get("timing_score", 50) < 50:
                _log_decision("skip", sig, f"bad entry timing {t['timing_score']}")
                return {"action": "skip", "signal": stype, "symbol": symbol,
                        "reason": f"entry timing poor ({t['timing_score']:.0f}/100) — wait"}
            sig["timing_score"] = t.get("timing_score", 50)
            sig["combined_score"] = combined_score(q["score"], t.get("timing_score", 50))
        except Exception:  # noqa: BLE001
            sig["timing_score"] = None
            sig["combined_score"] = q["score"]
    except Exception as e:  # noqa: BLE001
        _log_decision("skip", sig, f"scorer error {str(e)[:80]}")
        return {"action": "skip", "signal": stype, "symbol": symbol,
                "reason": "scorer unavailable — fail closed"}

    # Hard guards (universe, portfolio) already ran above — this is the
    # backstop in case control flow ever reaches here another way.
    ok, why = _guards_ok(symbol)
    if not ok:
        _log_decision("skip", sig, why)
        return {"action": "skip", "signal": stype, "symbol": symbol, "reason": why}

    # Cadence: don't double-buy the same symbol within the interval.
    # Interval comes from adaptive params (default 1 = DAILY, validated).
    ledger = load_ledger()
    try:
        from trading.signals.adaptive import load_params
        interval = load_params().get("interval_hours", 24)
    except Exception:  # noqa: BLE001
        interval = 24
    due, reason = should_entry(ledger, symbol, hours=interval)
    if not due:
        _log_decision("skip", sig, reason)
        return {"action": "skip", "signal": stype, "symbol": symbol,
                "reason": f"cadence: {reason}"}

    # Deep dip gets a bigger multiplier (2.0x) than normal dip (1.5x).
    # Use the signal's drawdown value (pre-computed by the detector) to avoid
    # a redundant API call. Fall back to live calc only if absent.
    dd = sig.get("drawdown_pct")
    if dd is None:
        from trading.allocation.testnet_executor import trailing_drawdown_pct
        dd = trailing_drawdown_pct(symbol, AllocationConfig().lookback_days)
    mult = 2.0 if stype == "deep_dip" else AllocationConfig().dip_multiplier
    amount = min(quote_amount * mult, MAX_ENTRY_USDT)

    try:
        result = run_entry(amount, symbol, force=False)
    except Exception as e:  # noqa: BLE001
        return {"action": "error", "signal": stype, "symbol": symbol, "reason": str(e)[:120]}

    if result.get("skipped"):
        return {"action": "skip", "signal": stype, "symbol": symbol,
                "reason": result.get("reason", "skipped")}

    rec = {
        "action": "trade", "signal": stype, "symbol": symbol,
        "amount_usdt": result["order"].get("cost"),
        "price": result["order"].get("price"),
        "qty": result["order"].get("amount"),
        "order_id": result["order"].get("id"),
        "multiplier": mult, "drawdown_pct": dd,
        "ts_utc": datetime.now(timezone.utc).isoformat(),
    }
    log = _load_log()
    log["trades"].append(rec)
    _save_log(log)
    return {"action": "trade", **rec}


def run_auto_trader(quote_amount: float = 100.0) -> dict:
    """Full auto-trader pass: scan signals, trade the tradeable ones."""
    new_signals = ms.scan_all()
    results = []
    for sig in new_signals:
        results.append(auto_trade_on_signal(sig, quote_amount))
    return {"signals_scanned": len(new_signals), "results": results,
            "ts_utc": datetime.now(timezone.utc).isoformat()}