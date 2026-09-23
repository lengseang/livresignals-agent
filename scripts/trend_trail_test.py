#!/usr/bin/env python3
"""TRAILING-STOP TEST — the one lever that protects an open gain.

THE PROBLEM
-----------
The real account holds 2 meaningful winners (+70% unrealised, ~+$8.4) with NO
profit protection. The only exits are:

    coin closes below SMA50        (a slow, far-away line)
    -15% from ENTRY                (a disaster stop, not a profit stop)

So a position that runs +100% can give essentially all of it back before either
exit triggers. The closed record shows exactly that shape: 8 of 9 exits were
`trend-broke`, and the run gave back gains rather than protecting them.

THE LEVER UNDER TEST
--------------------
Add an exit: price has fallen X% from its HIGHEST CLOSE since entry.

This is the classic trend-following trade-off. A tight trail banks gains but gets
shaken out of trends that would have continued; a loose trail does nothing until
it is too late. There is no universally right value, so the test reports the
whole curve and the cost of each choice, rather than picking a winner and
declaring it optimal.

WHAT WOULD MAKE THIS TEST WORTHLESS
----------------------------------
Choosing X after seeing the result. The grid is fixed in advance: 10/15/20/25/30
plus "no trail". The outcome is reported as-is, including where trailing loses.
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timezone

import ccxt

FEE_RT = 0.001594
SMA_FAST = 50
SMA_SLOW = 200
HARD_STOP = 0.15

REAL_SYMS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
             "ADA/USDT", "LINK/USDT", "DOGE/USDT", "AVAX/USDT", "NEAR/USDT",
             "SUI/USDT", "ARB/USDT", "DOT/USDT", "LTC/USDT", "ATOM/USDT"]


def sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


def fetch_daily(ex, sym, limit=400):
    try:
        return [(c[0], c[4]) for c in ex.fetch_ohlcv(sym, "1d", limit=limit)]
    except Exception as e:
        print(f"  fetch failed {sym}: {str(e)[:40]}", file=sys.stderr)
        return []


def run(data, btc, trail=None):
    """Long-only. Entry: coin>SMA50 and BTC>SMA200. Exit: coin<SMA50,
    -15% from entry, or trail% from the highest close since entry."""
    btc_c = [p[1] for p in btc]
    btc_ts = [p[0] for p in btc]
    rets, holds, give_back = [], [], []
    for sym, series in data.items():
        closes = [p[1] for p in series]
        ts = [p[0] for p in series]
        i = SMA_SLOW + 5
        pos = pos_i = None
        peak = None
        while i < len(closes):
            bi = None
            for j in range(len(btc_ts) - 1, -1, -1):
                if btc_ts[j] <= ts[i]:
                    bi = j
                    break
            if bi is None or bi < SMA_SLOW + 1:
                i += 1
                continue
            px = closes[i]
            if pos is None:
                c50 = sma(closes[:i + 1], SMA_FAST)
                b200 = sma(btc_c[:bi + 1], SMA_SLOW)
                if c50 and px > c50 and b200 and btc_c[bi] > b200:
                    pos, pos_i, peak = px, i, px
            else:
                peak = max(peak, px)
                c50 = sma(closes[:i + 1], SMA_FAST)
                hit_trend = c50 and px < c50
                hit_hard = px <= pos * (1 - HARD_STOP)
                hit_trail = (trail is not None
                             and px <= peak * (1 - trail))
                if hit_trend or hit_hard or hit_trail:
                    rets.append(px / pos - 1)
                    holds.append(i - pos_i)
                    # how much of the peak gain was surrendered?
                    give_back.append(1 - (px / pos - 1) / (peak / pos - 1)
                                     if peak > pos else 1.0)
                    pos = pos_i = peak = None
            i += 1
        if pos is not None:
            rets.append(closes[-1] / pos - 1)
            holds.append(len(closes) - 1 - pos_i)
            pk = max(peak, closes[-1])
            give_back.append(1 - (closes[-1] / pos - 1) / (pk / pos - 1)
                             if pk > pos else 1.0)
    return rets, holds, give_back


def stats(rets, holds, gb):
    if not rets:
        return None
    n = len(rets)
    avg = sum(rets) / n
    net = avg - FEE_RT
    sd = math.sqrt(sum((x - avg) ** 2 for x in rets) / (n - 1)) if n > 1 else 0
    t = avg / (sd / math.sqrt(n)) if sd else 0
    wr = sum(1 for x in rets if x > 0) / n * 100
    return (n, avg * 100, net * 100, t, wr, sum(holds) / n,
            sum(gb) / len(gb) * 100 if gb else 0)


def main():
    ex = ccxt.binance({"enableRateLimit": True})
    print("fetching ...", file=sys.stderr)
    btc = fetch_daily(ex, "BTC/USDT")
    data = {s: d for s in REAL_SYMS if (d := fetch_daily(ex, s))}
    print(f"  {len(data)} symbols | BTC "
          f"{datetime.fromtimestamp(btc[0][0]/1000, timezone.utc):%Y-%m-%d}"
          f"..{datetime.fromtimestamp(btc[-1][0]/1000, timezone.utc):%Y-%m-%d}",
          file=sys.stderr)

    print()
    print("=" * 98)
    print("TRAILING-STOP GRID — fixed in advance, reported as-is")
    print("=" * 98)
    print(f"  {'trail':14s} {'n':>4s} {'avg gross':>10s} {'net':>9s} "
          f"{'t':>6s} {'win%':>6s} {'avg hold':>9s} {'gain given back':>16s}")
    print("  " + "-" * 94)
    grid = [("none (current)", None), ("10%", 0.10), ("15%", 0.15),
            ("20%", 0.20), ("25%", 0.25), ("30%", 0.30)]
    res = []
    for label, tr in grid:
        r, h, g = run(data, btc, tr)
        st = stats(r, h, g)
        if not st:
            continue
        res.append((label, st))
        print(f"  {label:14s} {st[0]:4d} {st[1]:+9.3f}% {st[2]:+8.3f}% "
              f"{st[3]:+6.2f} {st[4]:5.1f}% {st[5]:8.1f}d {st[6]:15.1f}%")

    print()
    print("=" * 98)
    base = res[0][1]
    print(f"  baseline (no trail): {base[1]:+.3f}%/trade, win {base[4]:.0f}%, "
          f"gives back {base[6]:.1f}% of peak gain")
    best_net = max(res, key=lambda x: x[1][2])
    best_gb = min(res, key=lambda x: x[1][6])
    print(f"  best net           : {best_net[0]:14s} {best_net[1][2]:+.3f}%/trade")
    print(f"  least give-back    : {best_gb[0]:14s} {best_gb[1][6]:.1f}% given back")
    print()
    print("  READING THIS: a trail that raises net while cutting give-back is a")
    print("  genuine improvement. A trail that cuts give-back but LOWERS net has")
    print("  simply sold the winners early — it looks safer and makes less.")
    print()
    print("  ⚠️ 20 trades, one bull regime. Directional only.")


if __name__ == "__main__":
    main()
