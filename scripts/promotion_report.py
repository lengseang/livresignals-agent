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
import math
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "src"))

MIN_TRADES = 15
MIN_WR = 0.55
# Measured on this account, 33 real fills: 0.0797% per side with the BNB
# discount active. This is the number every candidate must beat.
FEE_RT = 0.001594
# What the toll becomes once the BNB runway ends (~110 fills left). A candidate
# that only clears the current fee is not robust; it is due to fail shortly.
FEE_RT_NOBNB = 0.002000
# Minimum t-statistic before a record is evidence rather than noise.
MIN_T = 2.0

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


print("=" * 94)
print("PROMOTION REPORT — paper record vs real-money eligibility")
print("=" * 94)
print(f"rule: >= {MIN_TRADES} paper trades, NET expectancy > 0 after the "
      f"{FEE_RT*100:.4f}% round-trip fee,")
print(f"      and |t| >= {MIN_T:.0f} so the record is evidence rather than noise")
print()
print("  ⚠️ WIN RATE IS NOT A GATE. It is reported, not enforced. A low win rate")
print("     with a large payoff is profitable (unified_trader: 47% WR, positive")
print("     and significant). A high win rate with a poor payoff is not")
print("     (regime_trader: 58% WR, and still loses money). Gating on win rate")
print("     would have rejected the only book that actually makes money.")
print()

stats = collect()
if not stats:
    print("no closed paper trades found in any known state file.")
    print("(the ledger needs history before it can promote anything — that is the")
    print(" point of widening SYMS: it starts collecting that history now.)")

rows = []
for s, rets in stats.items():
    n = len(rets)
    if n == 0:
        continue
    wr = sum(1 for r in rets if r > 0) / n
    avg = sum(rets) / n
    net = avg - FEE_RT
    net_nobnb = avg - FEE_RT_NOBNB
    sd = math.sqrt(sum((r - avg) ** 2 for r in rets) / (n - 1)) if n > 1 else 0
    t = avg / (sd / math.sqrt(n)) if sd else 0.0
    gains = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    payoff = ((sum(gains) / len(gains)) / abs(sum(losses) / len(losses))
              if gains and losses else float("inf"))
    rows.append((s, n, wr, avg, net, net_nobnb, t, payoff))
rows.sort(key=lambda r: -r[4])

print(f"  {'symbol':<13}{'n':>5}{'win%':>7}{'payoff':>8}{'gross':>9}"
      f"{'NET':>9}{'t':>7}   verdict")
print("  " + "-" * 90)
for s, n, wr, avg, net, net_nobnb, t, payoff in rows:
    if n < MIN_TRADES:
        v = f"need {MIN_TRADES - n} more trades"
    elif net <= 0:
        v = f"🔴 REJECT — loses {abs(net)*100:.3f}%/trade to fees"
    elif abs(t) < MIN_T:
        v = f"⏳ clears fees but UNPROVEN (t={t:+.2f} < {MIN_T:.0f})"
    elif net_nobnb <= 0:
        v = "⚠️ PROMOTE but THIN — dies when the BNB runway ends"
    else:
        v = "✅ PROMOTE" if s not in REAL_SYMS else "✅ already real"
    ps = f"{payoff:.2f}" if payoff != float("inf") else "inf"
    print(f"  {s:<13}{n:>5}{wr*100:>6.1f}%{ps:>8}{avg*100:>8.2f}%"
          f"{net*100:>8.2f}%{t:>+7.2f}   {v}")

print()
print("  BREAK-EVEN GROSS RETURN required: "
      f"{FEE_RT*100:.4f}% per trade "
      f"({FEE_RT_NOBNB*100:.4f}% after the BNB runway ends).")
print("  Anything under that line loses money no matter how good the signal.")

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
