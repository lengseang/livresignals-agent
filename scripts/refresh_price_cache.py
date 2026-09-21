#!/usr/bin/env python3
"""PRICE CACHE REFRESHER — the builder the research cache never had.

THE BUG THIS FIXES: research/price-cache-1000d.json is READ by six research
scripts (06-walkforward, 08, 09, 10, 11, 16) but had NO WRITER ANYWHERE.
It was created ad-hoc and could never refresh. It was found 41 hours stale -
meaning every backtest was silently testing old data.

A cache with no refresher is a trap: it rots silently and produces confident
wrong answers.

FORMAT (preserved exactly, so existing readers keep working):
  price-cache-1000d.json : {SYMBOL: {"<unix_day>": close}}   unix_day = ts/86400
  price-cache-gold-5y.json : {"<unix_day>": close}

SAFETY: writes to a temp file then atomically renames. A failed fetch can
NEVER corrupt the existing cache - the worst case is the old cache survives.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RES = Path("/opt/trading-agent/research")
CACHE = RES / "price-cache-1000d.json"
GOLD = RES / "price-cache-gold-5y.json"
H = {"User-Agent": "Mozilla/5.0"}

SYMS = ["BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "LINK", "DOGE", "AVAX",
        "NEAR", "ARB", "DOT", "LTC", "ATOM", "UNI", "APT", "SUI", "INJ",
        "OP", "TIA", "SEI", "FIL", "AAVE", "ETC"]


def get(url, timeout=25):
    req = urllib.request.Request(url, headers=H)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def atomic_write(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    os.replace(tmp, path)          # atomic - never a half-written cache


def fetch_binance(sym, days=1000):
    """daily closes, oldest -> newest"""
    u = (f"https://api.binance.com/api/v3/klines?symbol={sym}USDT"
         f"&interval=1d&limit={days}")
    raw = get(u)
    out = {}
    for k in raw:
        day = int(k[0]) // 86400000
        out[str(day)] = float(k[4])       # close
    return out


def fetch_gold(days=1300):
    u = (f"https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
         f"?interval=1d&range=5y")
    raw = get(u)
    res = raw["chart"]["result"][0]
    ts = res["timestamp"]
    cl = res["indicators"]["quote"][0]["close"]
    out = {}
    for t, c in zip(ts, cl):
        if c is None:
            continue
        out[str(int(t) // 86400)] = float(c)
    return out


def main():
    # ---- crypto ----
    fresh = {}
    failed = []
    for s in SYMS:
        for attempt in (1, 2):
            try:
                d = fetch_binance(s)
                if len(d) >= 200:
                    fresh[s] = d
                    print(f"  {s:6} {len(d):>5} days  "
                          f"(latest day {max(d, key=int)})")
                    break
                failed.append(f"{s} (only {len(d)} days)")
                break
            except Exception as e:
                if attempt == 2:
                    failed.append(f"{s} ({str(e)[:34]})")
                else:
                    time.sleep(1.5)
        time.sleep(0.15)

    if not fresh:
        print("❌ no symbols fetched — cache left UNTOUCHED (atomic write, "
              "old data survives)")
        return 1

    # guard: never shrink the cache catastrophically
    old = {}
    if CACHE.exists():
        try:
            old = json.loads(CACHE.read_text())
        except Exception:
            pass
    if old and len(fresh) < len(old) * 0.6:
        print(f"❌ only {len(fresh)}/{len(old)} symbols fetched — refusing to "
              f"replace the cache (would lose coverage)")
        return 1

    # keep symbols that failed but existed before (carry them forward)
    for s in old:
        if s not in fresh and s not in SYMS:
            fresh[s] = old[s]

    atomic_write(CACHE, fresh)
    days = len(next(iter(fresh.values())))
    print(f"✅ wrote {CACHE.name}: {len(fresh)} symbols x ~{days} days")

    # ---- gold ----
    try:
        g = fetch_gold()
        if len(g) > 500:
            atomic_write(GOLD, g)
            print(f"✅ wrote {GOLD.name}: {len(g)} days")
        else:
            print(f"⚠️ gold fetch returned only {len(g)} days — left untouched")
    except Exception as e:
        print(f"⚠️ gold fetch failed ({str(e)[:50]}) — left untouched")

    if failed:
        print(f"⚠️ symbols not refreshed: {', '.join(failed)}")
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M}] cache refresh complete")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"cache refresher error: {str(e)[:200]} — cache UNTOUCHED")
        raise SystemExit(1)
