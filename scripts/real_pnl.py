#!/usr/bin/env python3
"""REAL P&L — computed from the EXCHANGE, not from our own bookkeeping.

Why this exists
---------------
Every other report reads data/*.json, which is our own record of what we think
happened. This one asks Binance directly. The two disagreed: the state file
carried DOGE at qty=58 after an unrecorded partial sell had already reduced the
position to 23, so any P&L derived from the state file alone was fiction.

Ground truth beats bookkeeping. If this script and the state file disagree, the
state file is wrong.

Outputs
-------
  per-symbol realised P&L from actual fills (FIFO)
  unrealised P&L on what is still held, at the current mark
  the true account value

Reads credentials from config/live.env. Values are never printed.
"""
from __future__ import annotations

import json
from pathlib import Path

import ccxt

ROOT = Path("/opt/trading-agent")
SYMS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT", "ADA/USDT",
        "LINK/USDT", "DOGE/USDT", "AVAX/USDT", "NEAR/USDT", "SUI/USDT",
        "ARB/USDT", "DOT/USDT", "LTC/USDT", "ATOM/USDT",
        "AAVE/USDT", "ALGO/USDT", "APT/USDT", "AR/USDT", "AVA/USDT",
        "BCH/USDT", "CAKE/USDT", "CRV/USDT", "DASH/USDT", "ENA/USDT",
        "ETHFI/USDT", "FET/USDT", "FIL/USDT", "GALA/USDT", "HBAR/USDT",
        "INJ/USDT", "IOTA/USDT", "ONE/USDT", "OP/USDT", "PENDLE/USDT",
        "PEPE/USDT", "POL/USDT", "RAY/USDT", "RUNE/USDT", "SAGA/USDT",
        "SAND/USDT", "SEI/USDT", "SHIB/USDT", "SNX/USDT", "TAO/USDT",
        "TIA/USDT", "TRUMP/USDT", "TRX/USDT", "UNI/USDT", "VET/USDT",
        "WLD/USDT", "XLM/USDT", "ZEN/USDT"]


def creds():
    env = {}
    for line in (ROOT / "config" / "live.env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main():
    env = creds()
    ex = ccxt.binance({"apiKey": env.get("LIVE_API_KEY"),
                       "secret": env.get("LIVE_SECRET_KEY"),
                       "enableRateLimit": True})
    print("=" * 88)
    print("REAL P&L — from the EXCHANGE, not from our own records")
    print("=" * 88)

    realised_total = 0.0
    unreal_total = 0.0
    held_lines = []
    trade_lines = []

    for s in SYMS:
        try:
            tr = ex.fetch_my_trades(s, limit=200)
        except Exception:
            continue
        if not tr:
            continue
        # FIFO over fills
        buys = []
        realised = 0.0
        for t in sorted(tr, key=lambda x: x.get("timestamp") or 0):
            q = float(t["amount"])
            p = float(t["price"])
            fee = 0.0
            if t.get("fee") and t["fee"].get("cost"):
                fee = float(t["fee"]["cost"])
            if t.get("side") == "buy":
                buys.append([q, p, fee])
            else:
                left = q
                cost = 0.0
                fees_alloc = 0.0
                while left > 1e-12 and buys:
                    bq, bp, bf = buys[0]
                    take = min(left, bq)
                    cost += take * bp
                    fees_alloc += bf * (take / bq) if bq else 0
                    buys[0][0] -= take
                    left -= take
                    if buys[0][0] <= 1e-12:
                        buys.pop(0)
                proceeds = q * p - fee
                realised += proceeds - cost - fees_alloc
        held_q = sum(b[0] for b in buys)
        held_cost = sum(b[0] * b[1] for b in buys)
        if held_q > 1e-12:
            try:
                mark = float(ex.fetch_ticker(s)["last"])
            except Exception:
                mark = held_cost / held_q
            val = held_q * mark
            unreal_total += val - held_cost
            held_lines.append((s, held_q, held_cost / held_q, mark,
                               val - held_cost))
        if abs(realised) > 1e-9:
            realised_total += realised
            trade_lines.append((s, realised, len(tr)))

    if held_lines:
        print("\n  HELD NOW (unrealised)")
        for s, q, avg, mark, pnl in held_lines:
            print(f"    {s:11s} qty={q:<12.4f} avg={avg:<11.6f} "
                  f"now={mark:<11.6f} {pnl:+7.3f} USD")
    else:
        print("\n  nothing held")

    if trade_lines:
        print("\n  REALISED (closed, FIFO)")
        for s, pnl, n in sorted(trade_lines, key=lambda x: x[1]):
            print(f"    {s:11s} {pnl:+8.3f} USD over {n} fills")

    print("\n  " + "-" * 84)
    print(f"  realised total   : {realised_total:+8.3f} USD")
    print(f"  unrealised total : {unreal_total:+8.3f} USD")
    print(f"  NET              : {realised_total + unreal_total:+8.3f} USD")
    # fees actually paid
    try:
        for c in ("BNB", "USDT"):
            b = ex.fetch_balance()
            print(f"  free {c:4s}        : "
                  f"{float(b.get(c, {}).get('free', 0) or 0):.8f}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
