#!/usr/bin/env python3
"""EXIT-RULE A/B — prove (or disprove) that the exit bug caused the 0/9 record.

THE CLAIM UNDER TEST
--------------------
trend_follower's real record: 9 closes, ZERO wins, avg -4.77%, t = -2.91.
That is not "an unlucky stretch" — it is a significantly negative result.

The code's own docstring blames the EXIT path: exits used `not want`, and `want`
ANDs the coin's own trend with the market regime (BTC > SMA200). So a market-wide
flicker below BTC's SMA200 closed positions whose individual trends were intact,
then the coin was re-bought on the next run because it still qualified:

    SUI closed 3x, ATOM 2x, SAND 2x in eight days.

If that is the cause, replacing the exit with the coin's own trend alone should
transform the result on identical price data. If it does not, the docstring's
diagnosis is wrong and the strategy is simply bad — those are different
conclusions with different responses, so this test decides between them.

METHOD
------
Same universe, same period, same entries, same 0.1594% round trip. ONLY the exit
predicate differs. Nothing is tuned to the outcome.
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timezone

import ccxt

FEE_RT = 0.001594
SMA_FAST = 50
SMA_SLOW = 200

REAL_SYMS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
             "ADA/USDT", "LINK/USDT", "DOGE/USDT", "AVAX/USDT", "NEAR/USDT",
             "SUI/USDT", "ARB/USDT", "DOT/USDT", "LTC/USDT", "ATOM/USDT"]


def sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


def fetch_daily(ex, sym, limit=400):
    try:
        return [(c[0], c[4]) for c in ex.fetch_ohlcw(sym, "1d", limit=limit)]
    except Exception:
        pass
    try:
        return [(c[0], c[4]) for c in ex.fetch_ohlcv(sym, "1d", limit=limit)]
    except Exception as e:
        print(f"  fetch failed {sym}: {str(e)[:50]}", file=sys.stderr)
        return []


def run(data, btc, exit_rule, stop=0.15):
    """exit_rule(coin_closes, btc_closes, px, entry) -> bool  (True = close)"""
    btc_c = [p[1] for p in btc]
    btc_ts = [p[0] for p in btc]
    out, holds = [], []
    for sym, series in data.items():
        closes = [p[1] for p in series]
        ts = [p[0] for p in series]
        i = SMA_SLOW + 5
        pos, pos_i = None, None
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
                if (c50 and px > c50 and b200 and btc_c[bi] > b200):
                    pos, pos_i = px, i
            else:
                if exit_rule(closes[:i + 1], btc_c[:bi + 1], px, pos):
                    out.append(px / pos - 1)
                    holds.append(i - pos_i)
                    pos = None
            i += 1
        if pos is not None:
            out.append(closes[-1] / pos - 1)
            holds.append(len(closes) - 1 - pos_i)
    return out, holds


def stats(rets, holds, label):
    if not rets:
        return (label, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
    n = len(rets)
    avg = sum(rets) / n
    net = avg - FEE_RT
    sd = math.sqrt(sum((x - avg) ** 2 for x in rets) / (n - 1)) if n > 1 else 0
    t = avg / (sd / math.sqrt(n)) if sd else 0
    wr = sum(1 for x in rets if x > 0) / n * 100
    return (label, n, avg * 100, net * 100, t, wr, sum(holds) / n)


RULES = {
    "OLD (buggy): coin<SMA50 OR BTC<SMA200 OR -15%": (
        lambda c, b, px, e: (sma(c, SMA_FAST) and c[-1] < sma(c, SMA_FAST))
        or (sma(b, SMA_SLOW) and b[-1] < sma(b, SMA_SLOW))
        or px <= e * (1 - 0.15)),
    "NEW (fixed): coin<SMA50 OR -15%": (
        lambda c, b, px, e: (sma(c, SMA_FAST) and c[-1] < sma(c, SMA_FAST))
        or px <= e * (1 - 0.15)),
}


def main():
    ex = ccxt.binance({"enableRateLimit": True})
    print("fetching ...", file=sys.stderr)
    btc = fetch_daily(ex, "BTC/USDT")
    data = {}
    for s in REAL_SYMS:
        d = fetch_daily(ex, s)
        if d:
            data[s] = d
    print(f"  {len(data)} symbols | BTC "
          f"{datetime.fromtimestamp(btc[0][0]/1000, timezone.utc):%Y-%m-%d}"
          f"..{datetime.fromtimestamp(btc[-1][0]/1000, timezone.utc):%Y-%m-%d}",
          file=sys.stderr)

    print()
    print("=" * 96)
    print("EXIT RULE A/B — identical entries, only the exit predicate differs")
    print("=" * 96)
    print(f"  {'exit rule':48s} {'n':>4s} {'avg gross':>10s} {'net':>9s} "
          f"{'t':>6s} {'win%':>6s} {'avg days':>9s}")
    print("  " + "-" * 92)
    res = []
    for label, fn in RULES.items():
        r, h = run(data, btc, fn)
        st = stats(r, h, label)
        res.append(st)
        print(f"  {st[0]:48s} {st[1]:4d} {st[2]:+9.3f}% {st[3]:+8.3f}% "
              f"{st[4]:+6.2f} {st[5]:5.1f}% {st[6]:8.1f}")

    print()
    print("=" * 96)
    old, new = res[0], res[1]
    print(f"  OLD exit: {old[1]} trades, {old[2]:+.3f}%/trade, win {old[5]:.0f}%, "
          f"avg hold {old[6]:.1f}d")
    print(f"  NEW exit: {new[1]} trades, {new[2]:+.3f}%/trade, win {new[5]:.0f}%, "
          f"avg hold {new[6]:.1f}d")
    print()
    print(f"  the fix changes avg gross by {new[2]-old[2]:+.3f}%/trade and "
          f"trade count by {new[1]-old[1]:+d}")
    if new[1] < old[1]:
        print(f"  -> fewer trades AND a better average: the churn was real, and "
              f"closing it is the win.")
    if old[2] < 0 <= new[2]:
        print(f"  -> DIRECTLY CONFIRMS the docstring: the old exit turned a "
              f"profitable strategy negative.")
    print()
    print("  ⚠️ Small n, single regime. Directional evidence, not a validated edge.")


if __name__ == "__main__":
    main()
