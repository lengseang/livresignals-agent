"""Signal accuracy analytics — the learning loop's measurement layer.

Rolling accuracy (last N resolved signals), per-symbol accuracy, per-regime
accuracy, per-bias accuracy. Output feeds the daily report and the weekly
learning aggregate.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from trading.signals.signal_engine import _load_ledger


def resolved_signals() -> list[dict]:
    return [s for s in _load_ledger()["signals"] if s.get("outcome")]


def accuracy_stats(limit: int = 50) -> dict:
    """Rolling accuracy stats from resolved signals."""
    resolved = resolved_signals()
    if not resolved:
        return {"resolved": 0, "accuracy": None, "note": "no resolved signals yet"}

    recent = resolved[-limit:]
    correct = sum(1 for s in recent if s["outcome"]["correct"])
    per_symbol: dict[str, dict] = {}
    per_regime: dict[str, dict] = {}
    per_bias: dict[str, dict] = {}

    for s in resolved:
        for bucket, store in (
            (s["symbol"], per_symbol),
            (s["regime"], per_regime),
            (s["bias"], per_bias),
        ):
            b = store.setdefault(bucket, {"n": 0, "correct": 0})
            b["n"] += 1
            b["correct"] += int(s["outcome"]["correct"])

    def _fmt(d: dict[str, dict]) -> list[dict]:
        out = []
        for k, v in sorted(d.items(), key=lambda kv: -kv[1]["n"]):
            out.append({"bucket": k, "n": v["n"],
                        "accuracy": round(v["correct"] / v["n"] * 100, 1) if v["n"] else 0})
        return out

    return {
        "resolved": len(resolved),
        "recent_n": len(recent),
        "recent_correct": correct,
        "accuracy_pct": round(correct / len(recent) * 100, 1) if recent else 0.0,
        "per_symbol": _fmt(per_symbol),
        "per_regime": _fmt(per_regime),
        "per_bias": _fmt(per_bias),
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def daily_summary(day_start_utc: str | None = None) -> dict:
    """Signals generated today + their current resolution state."""
    import json
    from datetime import datetime
    ledger = _load_ledger()
    today = (day_start_utc or datetime.now(timezone.utc).date().isoformat())
    todays = [s for s in ledger["signals"] if s["ts_utc"][:10] == today]
    resolved_today = [s for s in todays if s.get("outcome")]
    return {
        "date": today,
        "signals_today": len(todays),
        "resolved_today": len(resolved_today),
        "accuracy_today": round(sum(1 for s in resolved_today if s["outcome"]["correct"]) / len(resolved_today) * 100, 1) if resolved_today else None,
        "by_action": _action_counts(todays),
        "signals": [
            {"id": s["id"], "symbol": s["symbol"], "action": s["action"], "bias": s["bias"],
             "price": s["price"], "drawdown_pct": s["drawdown_pct"], "regime": s["regime"],
             "outcome": s.get("outcome")}
            for s in todays
        ],
    }


def _action_counts(todays: list[dict]) -> dict:
    c: dict[str, int] = {}
    for s in todays:
        c[s["action"]] = c.get(s["action"], 0) + 1
    return c