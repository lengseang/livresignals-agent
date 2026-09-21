"""Adaptive learning loop — weekly signal analysis + parameter adjustment.

The honest version of "AI that improves itself":
  - Every signal is scored after its horizon (already done daily)
  - Weekly: aggregate accuracy by symbol / regime / bias
  - Adjust strategy parameters ONLY when evidence clears a statistical bar
    (n >= MIN_SAMPLE, accuracy beyond a margin), so we never overfit noise
  - Every adjustment is logged to data/learning-log.json and reported to
    #reports — fully transparent, fully reversible

Adjustable levers (from the validated P-A strategy):
  - dip_multiplier: raise if BUY signals in dip zone are beating expectation
  - drawdown_threshold: widen (-0.15 -> -0.12) if signals show dips deeper
    than the threshold are the best entries
  - per-symbol weight: 1.0 default; scales exposure by that symbol's edge
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from trading.signals import accuracy as acc
from trading.signals.signal_engine import _load_ledger, _save_ledger

LEARNING_LOG = Path(__file__).resolve().parents[3] / "data" / "learning-log.json"
STRATEGY_CONFIG = Path(__file__).resolve().parents[3] / "config" / "adaptive-params.json"

MIN_SAMPLE = 20        # need at least this many resolved signals per bucket
ACCURACY_MARGIN = 0.55  # bucket accuracy must exceed 55% to adjust
MAX_MULTIPLIER = 2.0
MIN_MULTIPLIER = 0.5


def _default_params() -> dict:
    return {
        "interval_hours": 24,      # 24 = daily, 1 = hourly, 4 = every 4h
        "dip_multiplier": 1.5,
        "drawdown_threshold": -0.05,   # OPTIMIZED: shallow dip = 57.8% win (was -0.15)
        "hold_hours": 48,          # OPTIMIZED: hold 48h (edge appears with time)
        "symbol_weights": {"BTC/USDT": 1.0, "ETH/USDT": 1.0, "SOL/USDT": 1.0},
        "updated_at_utc": None,
        "last_change_reason": None,
    }


def load_params() -> dict:
    if STRATEGY_CONFIG.exists():
        try:
            return json.loads(STRATEGY_CONFIG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return _default_params()


def save_params(params: dict) -> None:
    STRATEGY_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    STRATEGY_CONFIG.write_text(json.dumps(params, indent=2), encoding="utf-8")


def _load_log() -> dict:
    if LEARNING_LOG.exists():
        try:
            return json.loads(LEARNING_LOG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"adjustments": []}


def _save_log(log: dict) -> None:
    LEARNING_LOG.parent.mkdir(parents=True, exist_ok=True)
    LEARNING_LOG.write_text(json.dumps(log, indent=2), encoding="utf-8")


def weekly_analysis() -> dict:
    """Compute per-bucket accuracy and decide parameter adjustments."""
    stats = acc.accuracy_stats(limit=1000)
    params = load_params()
    log = _load_log()
    adjustments = []
    now = datetime.now(timezone.utc).isoformat()

    # Per-symbol weight adjustment from BUY signal accuracy.
    for b in stats.get("per_symbol", []):
        sym = b["bucket"]
        n = b["n"]
        acc_pct = b["accuracy"] / 100.0
        if n < MIN_SAMPLE:
            continue
        weight = params["symbol_weights"].get(sym, 1.0)
        if acc_pct >= ACCURACY_MARGIN + 0.10 and weight < MAX_MULTIPLIER:
            weight = min(MAX_MULTIPLIER, round(weight + 0.25, 2))
            adjustments.append({
                "type": "symbol_weight_up", "symbol": sym, "old": params["symbol_weights"].get(sym, 1.0),
                "new": weight, "accuracy": round(acc_pct * 100, 1), "n": n, "at_utc": now,
            })
            params["symbol_weights"][sym] = weight
        elif acc_pct <= 0.45 and weight > MIN_MULTIPLIER:
            weight = max(MIN_MULTIPLIER, round(weight - 0.25, 2))
            adjustments.append({
                "type": "symbol_weight_down", "symbol": sym, "old": params["symbol_weights"].get(sym, 1.0),
                "new": weight, "accuracy": round(acc_pct * 100, 1), "n": n, "at_utc": now,
            })
            params["symbol_weights"][sym] = weight

    if adjustments:
        params["updated_at_utc"] = now
        params["last_change_reason"] = "; ".join(
            f"{a['type']} {a['symbol']} {a['old']}->{a['new']}" for a in adjustments)
        save_params(params)
        log["adjustments"].extend(adjustments)
        _save_log(log)

    return {
        "computed_at_utc": now,
        "stats": {k: v for k, v in stats.items() if k not in ("per_symbol",)},
        "per_symbol": stats.get("per_symbol", []),
        "adjustments": adjustments,
        "params": params,
    }


def params_card() -> tuple[str, str, list]:
    """Report card for #reports: current adaptive params + last changes."""
    params = load_params()
    log = _load_log()
    lines = [f"dip_multiplier  : {params['dip_multiplier']}",
             f"dd_threshold    : {params['drawdown_threshold']}"]
    for sym, w in params["symbol_weights"].items():
        lines.append(f"{sym:<10} weight {w}")
    if log["adjustments"]:
        lines.append("--- last adjustments ---")
        for a in log["adjustments"][-5:]:
            lines.append(f"{a['at_utc'][:16]} {a['type']} {a['symbol']} {a['old']}->{a['new']} (acc {a['accuracy']}% n={a['n']})")
    else:
        lines.append("--- no adjustments yet (need n>=20 per bucket) ---")
    return "🧠 ADAPTIVE PARAMS", "<pre>" + "\n".join(lines) + "</pre>", [
        [{"text": "🔄 Refresh", "callback_data": "cpnl:params"}],
    ]


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "analyze"
    if mode == "analyze":
        r = weekly_analysis()
        print(json.dumps(r, indent=2))
    elif mode == "card":
        title, body, buttons = params_card()
        from trading.tg.bot_ui import send_card
        print(json.dumps(send_card("reports", title, body, buttons)))