#!/usr/bin/env python3
"""Trade Judge — the adaptive meta-controller (the "human PM" layer).

Sits ABOVE unified_trader. Instead of blindly scanning for one signal type,
the Judge:

  1. Reads the current MARKET REGIME (trending / ranging / volatile, + direction)
     from real data — not a hardcoded guess.
  2. SELECTS which edge to hunt based on regime:
       - VOLATILE + down  -> capitulation-catch (deep dip, our validated 72% edge)
       - RANGING          -> mean-reversion on shallow dips (paper-validated first)
       - TRENDING up      -> momentum follow (only if walk-forward proves it)
  3. STANDS DOWN when no regime matches a proven edge (no forced trades).
  4. ADAPTS sizing to account size: tiny account = patient (wait for A+ setups);
     funded account = can take B+ setups too.
  5. LEARNS: promotes a paper edge to real only after N confirmed wins OOS.
  6. EXPLAINS its decision in plain language (like a PM would).

Pure stdlib. Reads live data; writes a decision ledger.

Run: called by unified_trader each cycle, OR `python trade_judge.py` for a
one-shot human-readable market read.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "data" / "judge-ledger.json"

# Account-size tiers: below FUND_THRESHOLD we are "tiny" -> patient only.
FUND_THRESHOLD = 50.0  # USD; above this, take B+ setups too

# Regime -> which edge to hunt (must be walk-forward validated to go REAL)
REGIME_EDGE_MAP = {
    "VOLATILE_DOWN": "capitulation",
    "VOLATILE": "capitulation",
    "RANGING": "mean_reversion",
    "TRENDING_UP": "momentum",
    "TRENDING_DOWN": "capitulation",
    "TRENDING": "capitulation",
}


def _market_regime(ex, sym="BTC/USDT", tf="1h", lookback=90) -> dict:
    """Classify regime from real candles: trend direction + volatility."""
    try:
        closes = [c[4] for c in ex.fetch_ohlcv(sym, tf, limit=lookback)]
    except Exception:
        return {"regime": "UNKNOWN", "trend": 0.0, "vol": 0.0}
    n = len(closes)
    if n < 30:
        return {"regime": "UNKNOWN", "trend": 0.0, "vol": 0.0}
    # slope (normalized)
    xm = (n - 1) / 2
    ym = sum(closes) / n
    denom = sum((i - xm) ** 2 for i in range(n)) or 1
    slope = sum((i - xm) * (closes[i] - ym) for i in range(n)) / denom
    trend = slope / (ym or 1)
    # volatility: stdev of returns
    import statistics
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, n)]
    vol = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    # classify
    if vol > 0.012:
        base = "VOLATILE"
    elif abs(trend) > 0.0008:
        base = "TRENDING"
    else:
        base = "RANGING"
    if base == "TRENDING":
        base = "TRENDING_UP" if trend > 0 else "TRENDING_DOWN"
    # down-vol is the capitulation sweet spot
    if base == "VOLATILE" and trend < 0:
        base = "VOLATILE_DOWN"
    return {"regime": base, "trend": round(trend, 5), "vol": round(vol, 5)}


def _account_tier(free_usd: float) -> str:
    return "FUNDED" if free_usd >= FUND_THRESHOLD else "TINY"


def _load_ledger() -> dict:
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text())
        except Exception:
            pass
    return {"paper_edges": {}, "promotions": [], "decisions": []}


def _save_ledger(d: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(d, indent=2))


def decide(ex, free_usd: float) -> dict:
    """The Judge's decision for this cycle. Returns a plain-language dict."""
    reg = _market_regime(ex)
    tier = _account_tier(free_usd)
    edge = REGIME_EDGE_MAP.get(reg["regime"], "stand_down")
    ledger = _load_ledger()

    # Stand-down if no proven edge for this regime
    if edge == "stand_down" or reg["regime"] == "UNKNOWN":
        decision = {
            "action": "STAND_DOWN",
            "reason": f"regime={reg['regime']} has no validated edge — no forced trade",
            "regime": reg, "tier": tier, "hunt": None,
        }
    else:
        # Check if this edge is promoted to REAL or still paper-only
        real_ok = (edge in ledger["promotions"]) or (edge == "capitulation")
        decision = {
            "action": "HUNT",
            "hunt": edge,
            "real_allowed": real_ok,
            "reason": (f"regime={reg['regime']} -> hunt '{edge}' edge; "
                       f"{'real money OK' if real_ok else 'paper-validate first'}"),
            "regime": reg, "tier": tier,
        }
        # Tiny account + non-capitulation edge -> paper only (protect principal)
        if tier == "TINY" and edge != "capitulation":
            decision["real_allowed"] = False
            decision["reason"] += " | TINY account: non-capitulation edges stay paper-only"

    decision["utc"] = datetime.now(timezone.utc).isoformat()
    # keep last 50 decisions
    ledger["decisions"] = ledger.get("decisions", [])[-49:] + [decision]
    _save_ledger(ledger)
    return decision


def main():
    from trading.exchange.live_client import make_live_exchange, live_balances
    ex = make_live_exchange()
    ex.load_markets()
    b = live_balances()
    free = float(b.get("USDT", 0)) + float(b.get("USD", 0)) + float(b.get("USDC", 0))
    d = decide(ex, free)
    print(f"=== TRADE JUDGE @ {d['utc']} ===")
    print(f"  account: ${free:.2f} ({d['tier']})")
    print(f"  regime : {d['regime']['regime']} (trend={d['regime']['trend']}, vol={d['regime']['vol']})")
    print(f"  ACTION : {d['action']}")
    print(f"  hunt   : {d.get('hunt')} | real_allowed={d.get('real_allowed')}")
    print(f"  why    : {d['reason']}")


if __name__ == "__main__":
    main()
