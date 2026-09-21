#!/usr/bin/env python3
"""Promotion report — which paper coins have earned real money?

The rule (from the symbol-tier gate in unified_trader.py, 2026-09-09):
    promote a coin to REAL_SYMS only when it shows >= 55% win rate
    over >= 15 recorded paper trades.

Rationale: per-SYMBOL stats matter as much as per-edge stats. ARB had a +39% day
and is still the biggest loser on the book (31% WR / -$0.32) — "hot" is not
"profitable". Widening SYMS buys data; REAL_SYMS is where money is allowed.

Read-only. It never trades and never edits REAL_SYMS; it prints what you should
copy into scripts/trend_follower.py.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "src"))

MIN_TRADES = 15
MIN_WR = 0.55

from trend_follower import SYMS, REAL_SYMS          # noqa: E402


def load(name):
    try:
        return json.loads((BASE / "data" / name).read_text(encoding="utf-8"))
    except Exception:
        return None


def collect():
    """Every closed paper trade we can find, as (symbol, ret)."""
    out = defaultdict(list)

    ts = load("trend-state.json") or {}
    for t in ts.get("log", []):
        s, r = t.get("symbol"), t.get("ret")
        if s and isinstance(r, (int, float)):
            out[s].append(float(r))

    for fname, keys in (("unified-paper.json", ("closed_trades", "closed")),
                        ("pa-paper-state.json", ("closed_trades", "closed")),
                        ("adaptive-state.json", ("closed_trades",))):
        d = load(fname) or {}
        for k in keys:
            for t in (d.get(k) or []):
                if not isinstance(t, dict):
                    continue
                s = t.get("symbol") or t.get("pair")
                r = t.get("ret")
                if r is None:
                    e, x = t.get("entry"), t.get("exit") or t.get("exit_px")
                    if e and x:
                        try:
                            r = float(x) / float(e) - 1
                        except Exception:
                            r = None
                if s and isinstance(r, (int, float)):
                    out[s].append(float(r))
    return out


print("=" * 78)
print("PROMOTION REPORT — paper record vs real-money eligibility")
print("=" * 78)
print(f"rule: >= {MIN_TRADES} paper trades AND >= {MIN_WR*100:.0f}% win rate\n")

stats = collect()
if not stats:
    print("no closed paper trades found in any known state file.")
    print("(the ledger needs history before it can promote anything — that is the")
    print(" point of widening SYMS: it starts collecting that history now.)")

rows = []
for s, rets in stats.items():
    n = len(rets)
    wr = sum(1 for r in rets if r > 0) / n
    avg = sum(rets) / n
    rows.append((s, n, wr, avg))
rows.sort(key=lambda r: (-r[2], -r[1]))

print(f"{'symbol':<14}{'n':>5}{'win%':>8}{'avg/trade':>11}   verdict")
print("-" * 78)
for s, n, wr, avg in rows:
    if n >= MIN_TRADES and wr >= MIN_WR and avg > 0:
        v = "PROMOTE" if s not in REAL_SYMS else "already real"
    elif n >= MIN_TRADES and wr >= MIN_WR:
        v = "reject (WR ok, expectancy <= 0)"
    elif n < MIN_TRADES:
        v = f"need {MIN_TRADES - n} more trades"
    else:
        v = "reject (WR too low)"
    print(f"{s:<14}{n:>5}{wr*100:>7.1f}%{avg*100:>10.2f}%   {v}")

print()
print("=" * 78)
print("DEAD-SYMBOL CHECK — every SYMS entry must still be tradable")
print("=" * 78)
try:
    from trading.exchange.live_client import make_live_exchange
    info = make_live_exchange().publicGetExchangeInfo()
    live = {m["symbol"] for m in info["symbols"]
            if m.get("status") == "TRADING" and m.get("isSpotTradingAllowed")}
    dead = [s for s in SYMS if s.replace("/", "") not in live]
    if dead:
        print(f"🔴 DEAD in SYMS: {dead}")
        print("   each one costs an API call and a silent `continue` every cycle.")
    else:
        print(f"✅ all {len(SYMS)} SYMS entries are tradable")
    missing = [s for s in REAL_SYMS if s.replace("/", "") not in live]
    if missing:
        print(f"🔴 DEAD in REAL_SYMS: {missing}")
except Exception as e:
    print(f"could not reach the exchange: {type(e).__name__}: {str(e)[:80]}")

print()
print("=" * 78)
print(f"universe: {len(SYMS)} paper coins | {len(REAL_SYMS)} real-money coins "
      f"| {len([s for s in SYMS if s not in REAL_SYMS])} paper-only")
print("=" * 78)
print("to promote: add the symbol to REAL_SYMS in scripts/trend_follower.py")
print("and to ALLOWED_REAL / lot rounding in live_executor.py.")
