#!/usr/bin/env python3
"""Pull all-timeframe data for liquid symbols, saving incrementally (resumable)."""
import json
import os
import time

import sys
sys.path.insert(0, "/opt/trading-agent/src")
from trading.data.ccxt_fetcher import make_exchange

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "DOGE/USDT",
    "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT", "SHIB/USDT", "LTC/USDT",
    "BCH/USDT", "UNI/USDT", "ATOM/USDT", "XLM/USDT", "NEAR/USDT", "APT/USDT",
    "ARB/USDT", "OP/USDT", "FIL/USDT", "ICP/USDT", "INJ/USDT", "SUI/USDT",
    "SEI/USDT", "TIA/USDT", "PEPE/USDT", "WIF/USDT", "TRX/USDT", "XRP/USDT",
]
TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h"]
OUT = "/opt/trading-agent/data/full_sweep.json"

# resume from existing partial file
out = {}
if os.path.exists(OUT):
    try:
        out = json.load(open(OUT))
        print(f"resuming — {len(out)} series already done", flush=True)
    except Exception:
        out = {}

ex = make_exchange(sandbox=True)
since = ex.parse8601("2023-01-01T00:00:00Z")
now = ex.milliseconds()

for sym in SYMBOLS:
    for tf in TIMEFRAMES:
        key = f"{tf}:{sym}"
        if key in out:
            continue
        all_c = []
        cur = since
        guard = 0
        while cur < now and guard < 400:
            try:
                batch = ex.fetch_ohlcv(sym, tf, since=cur, limit=1000)
            except Exception as e:
                print(f"{key} err: {str(e)[:60]}", flush=True)
                time.sleep(1.5)
                continue
            if not batch:
                break
            all_c.extend(batch)
            cur = batch[-1][0] + 1
            guard += 1
            if len(batch) < 1000:
                break
            time.sleep(0.03)
        out[key] = [[c[0], c[4]] for c in all_c]
        # save after every series so nothing is lost
        json.dump(out, open(OUT, "w"))
        print(f"saved {key}: {len(all_c)} candles ({len(out)}/{len(SYMBOLS)*len(TIMEFRAMES)})", flush=True)

print("DONE — full_sweep.json complete", flush=True)
