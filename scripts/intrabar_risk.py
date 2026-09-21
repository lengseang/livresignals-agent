#!/usr/bin/env python3
"""Intra-hour gap risk investigation.

Question: if we only check price once per hour, how often does BTC hit SL
before TP *within* an hour, that we'd miss by only looking at hourly closes?

Using the fine data we already have (30m/15m), we reconstruct the intrabar
path with HIGH and LOW to see: for a given TP/SL, does SL get hit before TP
inside a bar, even when the bar CLOSE looks fine?

This answers: is hourly monitoring safe, or do we need faster checks?
"""
from __future__ import annotations

import json
import statistics

FINE = "/home/ubuntu/../opt/trading-agent/data/live_fine.json"


def load(tf):
    import json as j
    # live_fine.json stores [ts, close] only — no high/low.
    # Fall back to re-deriving from a fresh fetch if needed.
    p = "/opt/trading-agent/data/live_fine.json"
    d = j.load(open(p))
    return d[tf]


def main():
    # NOTE: our stored fine data only has [ts, close], not high/low.
    # To measure intrabar SL-before-TP, we need high/low. Fetch fresh with full OHLC.
    from trading.data.ccxt_fetcher import make_exchange
    ex = make_exchange(sandbox=True)

    print("=== Intrabar SL-vs-TP risk (BTC 30m, using HIGH/LOW) ===\n")
    for tf, bar_min in [("30m", 30), ("15m", 15)]:
        ohlcv = ex.fetch_ohlcv("BTC/USDT", tf, limit=500)
        # ohlcv = [ts, open, high, low, close, vol]
        results = []
        for stop_pct in [0.5, 0.75, 1.0, 1.5]:   # SL distance %
            for rr in [2.0]:                       # reward:risk 2:1
                tp_pct = stop_pct * rr
                sl_first = 0
                tp_first = 0
                neither = 0
                # simulate entries at each bar open, check intrabar high/low
                for i in range(1, len(ohlcv)):
                    o = float(ohlcv[i][1])
                    h = float(ohlcv[i][2])
                    l = float(ohlcv[i][3])
                    if o <= 0:
                        continue
                    sl_price = o * (1 - stop_pct / 100)
                    tp_price = o * (1 + tp_pct / 100)
                    # intrabar: did it touch SL or TP first?
                    hit_sl = l <= sl_price
                    hit_tp = h >= tp_price
                    if hit_sl and hit_tp:
                        # both touched — ambiguous; approximate by which is closer to open
                        # (can't know order without tick data; flag as "both")
                        neither += 1
                    elif hit_sl:
                        sl_first += 1
                    elif hit_tp:
                        tp_first += 1
                    else:
                        neither += 1
                total = sl_first + tp_first + neither
                results.append((stop_pct, rr, sl_first, tp_first, neither, total))
        print(f"--- {tf} ---")
        print(f"{'SL%':>5} {'RR':>4} {'SL first':>9} {'TP first':>9} {'neither/both':>13} {'n':>6}")
        for stop_pct, rr, sf, tf_, ne, tot in results:
            print(f"{stop_pct:>5.1f} {rr:>4.1f} {sf:>9} {tf_:>9} {ne:>13} {tot:>6}")
        print()


if __name__ == "__main__":
    main()
