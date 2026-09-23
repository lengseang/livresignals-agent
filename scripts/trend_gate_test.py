#!/usr/bin/env python3
"""TREND GATE TEST — does a stricter regime filter remove the losses?

THE HYPOTHESIS
--------------
The real trend book closed 9 trades with ZERO wins, averaging -4.77%. Every exit
was either `trend-broke` or the -15% hard stop, i.e. the book kept buying coins
just as the market rolled over and getting shaken out.

The current entry gate is:

    coin > SMA50(coin)   AND   BTC > SMA200(BTC)

The claim under test: BTC > SMA200 is too loose a definition of "a market worth
being long in". BTC spent the period chopping around its SMA200, so the gate
kept opening during exactly the conditions that whipsaw a trend follower.

METHOD
------
Replay daily closes for the real universe over the period the losses occurred and
compare gate variants on identical data, net of the measured 0.1594% round trip.
No parameter is fitted to the result; the variants are chosen a priori and the
comparison is reported as-is, including when it shows no improvement.

CAVEAT, STATED UP FRONT
-----------------------
This is a small sample on one market regime. It can show that a gate would have
helped HERE; it cannot prove it helps in general. Treat the output as evidence
about a specific period, not a validated edge.
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from pathlib import Path

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
        o = ex.fetch_ohlcv(sym, "1d", limit=limit)
        return [(c[0], c[4]) for c in o]      # (ts_ms, close)
    except Exception as e:
        print(f"  fetch failed {sym}: {str(e)[:50]}")
        return []


def run(data, btc, gate, stop=0.15):
    """Replay. gate(close_history, btc_close_history) -> bool entry allowed.

    Long-only, one position per symbol at a time, exit when the coin's own
    SMA50 breaks or the hard stop triggers. Returns list of trade returns.
    """
    btc_c = [p[1] for p in btc]
    btc_ts = [p[0] for p in btc]
    out = []
    for sym, series in data.items():
        closes = [p[1] for p in series]
        ts = [p[0] for p in series]
        i = SMA_SLOW + 5
        pos = None
        while i < len(closes):
            # align btc history to this day
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
                if (c50 and px > c50
                        and gate(closes[:i + 1], btc_c[:bi + 1])):
                    pos = px
            else:
                c50 = sma(closes[:i + 1], SMA_FAST)
                if (c50 and px < c50) or px <= pos * (1 - stop):
                    out.append(px / pos - 1)
                    pos = None
            i += 1
        if pos is not None:
            out.append(closes[-1] / pos - 1)   # mark open trades too
    return out


def stats(rets, label):
    if not rets:
        return (label, 0, 0.0, 0.0, 0.0, 0.0)
    n = len(rets)
    avg = sum(rets) / n
    net = avg - FEE_RT
    sd = math.sqrt(sum((x - avg) ** 2 for x in rets) / (n - 1)) if n > 1 else 0
    t = avg / (sd / math.sqrt(n)) if sd else 0
    wr = sum(1 for x in rets if x > 0) / n * 100
    return (label, n, avg * 100, net * 100, t, wr)


def main():
    ex = ccxt.binance({"enableRateLimit": True})
    print("fetching daily closes ...", file=sys.stderr)
    btc = fetch_daily(ex, "BTC/USDT")
    data = {}
    for s in REAL_SYMS:
        d = fetch_daily(ex, s)
        if d:
            data[s] = d
    print(f"  {len(data)} symbols, {len(btc)} BTC days "
          f"({datetime.fromtimestamp(btc[0][0]/1000, timezone.utc):%Y-%m-%d}"
          f" .. {datetime.fromtimestamp(btc[-1][0]/1000, timezone.utc):%Y-%m-%d})",
          file=sys.stderr)

    gates = {
        "A. current: BTC > SMA200":
            lambda c, b: sma(b, SMA_SLOW) is not None and b[-1] > sma(b, SMA_SLOW),
        "B. stricter: BTC > SMA200 AND BTC > SMA50":
            lambda c, b: (sma(b, SMA_SLOW) and b[-1] > sma(b, SMA_SLOW)
                          and sma(b, SMA_FAST) and b[-1] > sma(b, SMA_FAST)),
        "C. momentum: BTC > SMA200 AND BTC up over 30d":
            lambda c, b: (sma(b, SMA_SLOW) and b[-1] > sma(b, SMA_SLOW)
                          and len(b) > 31 and b[-1] > b[-31]),
        "D. no regime filter at all":
            lambda c, b: True,
    }

    print()
    print("=" * 92)
    print("GATE COMPARISON — same universe, same period, net of 0.1594% round trip")
    print("=" * 92)
    print(f"  {'gate':46s} {'n':>4s} {'avg gross':>10s} {'net':>9s} "
          f"{'t':>6s} {'win%':>6s}")
    print("  " + "-" * 88)
    res = []
    for label, g in gates.items():
        r = run(data, btc, g)
        st = stats(r, label)
        res.append(st)
        print(f"  {st[0]:46s} {st[1]:4d} {st[2]:+9.3f}% {st[3]:+8.3f}% "
              f"{st[4]:+6.2f} {st[5]:5.1f}%")

    print()
    print("=" * 92)
    best = max(res, key=lambda x: x[3])
    cur = [r for r in res if r[0].startswith("A.")][0]
    print(f"  current gate A : n={cur[1]}  net {cur[3]:+.3f}%/trade  win {cur[5]:.0f}%")
    print(f"  best variant   : {best[0]}")
    print(f"                   n={best[1]}  net {best[3]:+.3f}%/trade  win {best[5]:.0f}%")
    delta = best[3] - cur[3]
    print(f"  improvement    : {delta:+.3f}%/trade "
          f"({'better' if delta > 0 else 'worse or equal'})")
    print()
    print("  ⚠️ one regime, small n. Evidence for this period, not a validated edge.")


if __name__ == "__main__":
    main()
