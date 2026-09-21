#!/usr/bin/env python3
"""Earning-rate projection — honest numbers for the frequency-optimized strategy.

Uses the validated, embargoed results:
  - Loose config (vol .006 + 1 red + 24h): +69.8% compounded / 1.5yr, 73 trades
  - Net of fees (0.2% round trip): ~+55% / 1.5yr
  - ~4.1 trades/month

Projects what this ACTUALLY earns at different capital levels, so the user
sees honest coffee-money → income math instead of a win-rate fantasy.
"""
from __future__ import annotations

# Validated numbers (2026-08-13, embargoed, BTC 1h, 1.5yr)
TRADES_PER_YEAR = 73 / 1.5          # ~48.7 trades/year
AVG_RET_PER_TRADE = 0.00785         # +0.785% gross
FEE_PER_TRADE = 0.002               # 0.2% round trip
NET_PER_TRADE = AVG_RET_PER_TRADE - FEE_PER_TRADE  # ~+0.585%

def project(months: int = 12):
    trades = TRADES_PER_YEAR * (months / 12)
    net_per = NET_PER_TRADE
    out = []
    for capital in [100, 500, 1000, 5000, 10000]:
        # simple: capital * (1 + net_per)^trades (compounded per trade)
        final = capital * (1 + net_per) ** trades
        profit = final - capital
        out.append({
            "capital": capital,
            "final": round(final, 2),
            "profit": round(profit, 2),
            "monthly": round(profit / months, 2),
            "daily": round(profit / (months * 30), 2),
        })
    return trades, out


if __name__ == "__main__":
    trades, rows = project(12)
    print(f"Honest earning projection (12 months, frequency-optimized strategy)")
    print(f"  ~{trades:.0f} trades/year, +{NET_PER_TRADE*100:.2f}% net/trade (after 0.2% fees)")
    print()
    print(f"{'Capital':>8} {'→ Final':>10} {'Profit/yr':>10} {'$/month':>9} {'$/day':>8}")
    for r in rows:
        print(f"${r['capital']:>7,} ${r['final']:>9,.0f} ${r['profit']:>9,.0f} "
              f"${r['monthly']:>8,.2f} ${r['daily']:>7,.2f}")
    print()
    print("  NOTE: these are PROJECTIONS from 1.5yr backtest. Real results vary.")
    print("  $100 → coffee money. $1000+ → meaningful income. Same edge, bigger base.")
