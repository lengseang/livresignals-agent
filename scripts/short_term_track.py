#!/usr/bin/env python3
"""Track B — Short-term quick-trade simulator (paper).

Runs the intraday strategy in parallel with the long-term accumulation:
  - Uses the planner (entry/TP/SL/P(win)/EV) on each signal
  - Simulates opening a position when the plan says BUY
  - Closes on TP or SL within hours (intraday horizon)
  - Tracks a SEPARATE paper budget + P&L, so we can compare the two tracks
    honestly: does "quick money" actually beat "slow and steady"?

Budget: starts at $500 paper. Each trade risks a Kelly-sized fraction.
Every trade is logged with entry/exit/reason so the accuracy loop learns.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading.signals import multi_signal  # noqa: E402
from trading.signals.planner import plan_trade  # noqa: E402

STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "short-term-state.json"


def _load() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "budget": 500.0,           # paper starting budget for this track
        "open_positions": [],      # [{symbol, entry, tp, sl, size, ts_ms}]
        "closed_trades": [],       # [{symbol, entry, exit, pnl, reason, ts}]
        "last_check_utc": None,
    }


def _save(s: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")


def check_and_trade() -> dict:
    """One short-term cycle: close hit TP/SL, open new quality trades."""
    s = _load()
    events = []

    # 1. Close any open position whose TP or SL was hit (live price).
    try:
        ex = multi_signal.market_features  # reuse the fetcher
        prices = {}
        for pos in s["open_positions"]:
            sym = pos["symbol"]
            if sym not in prices:
                from trading.data.ccxt_fetcher import make_exchange
                e = make_exchange(sandbox=True)
                prices[sym] = float(e.fetch_ticker(sym)["last"])
            px = prices[sym]
            if px >= pos["tp"]:
                pnl = (px - pos["entry"]) / pos["entry"] * pos["size"]
                s["budget"] += pos["size"] + pnl
                s["closed_trades"].append({"symbol": sym, "entry": pos["entry"],
                                           "exit": px, "pnl": round(pnl, 4),
                                           "reason": "TP", "ts_utc": datetime.now(timezone.utc).isoformat()})
                events.append(f"TP hit {sym} +${pnl:.2f}")
            elif px <= pos["sl"]:
                pnl = (px - pos["entry"]) / pos["entry"] * pos["size"]
                s["budget"] += pos["size"] + pnl
                s["closed_trades"].append({"symbol": sym, "entry": pos["entry"],
                                           "exit": px, "pnl": round(pnl, 4),
                                           "reason": "SL", "ts_utc": datetime.now(timezone.utc).isoformat()})
                events.append(f"SL hit {sym} ${pnl:.2f}")
        s["open_positions"] = [p for p in s["open_positions"]
                               if p["symbol"] not in {c["symbol"] for c in s["closed_trades"][-5:]}]
    except Exception as e:  # noqa: BLE001
        events.append(f"price check error: {str(e)[:80]}")

    # 2. Open new trades when a quality shallow-dip signal fires.
    try:
        new_signals = multi_signal.scan_all()
        for sig in new_signals:
            if sig["signal_type"] not in ("dip_zone",):
                continue
            plan = plan_trade(sig, bankroll_usdt=s["budget"])
            if plan.action != "BUY":
                continue
            # Only open if not already holding this symbol.
            if any(p["symbol"] == sig["symbol"] for p in s["open_positions"]):
                continue
            size = min(plan.size_usdt, s["budget"] * 0.25)
            if size < 5.0:  # below minimum meaningful size
                continue
            s["open_positions"].append({
                "symbol": sig["symbol"], "entry": plan.entry, "tp": plan.tp,
                "sl": plan.sl, "size": size, "ts_ms": int(time.time() * 1000),
            })
            s["budget"] -= size
            events.append(f"opened {sig['symbol']} @ {plan.entry} (TP {plan.tp} / SL {plan.sl}, size ${size:.2f})")
    except Exception as e:  # noqa: BLE001
        events.append(f"signal error: {str(e)[:80]}")

    s["last_check_utc"] = datetime.now(timezone.utc).isoformat()
    _save(s)
    return {"budget": round(s["budget"], 2), "open": len(s["open_positions"]),
            "closed": len(s["closed_trades"]), "events": events}


def summary() -> dict:
    s = _load()
    wins = sum(1 for t in s["closed_trades"] if t["pnl"] > 0)
    losses = sum(1 for t in s["closed_trades"] if t["pnl"] <= 0)
    total_pnl = sum(t["pnl"] for t in s["closed_trades"])
    return {
        "budget": round(s["budget"], 2),
        "starting_budget": 500.0,
        "open_positions": len(s["open_positions"]),
        "closed_trades": len(s["closed_trades"]),
        "wins": wins, "losses": losses,
        "win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) else None,
        "total_pnl": round(total_pnl, 2),
        "net_return_pct": round(total_pnl / 500.0 * 100, 2),
    }


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "trade"
    if mode == "trade":
        print(json.dumps(check_and_trade(), indent=2))
    elif mode == "summary":
        print(json.dumps(summary(), indent=2))
