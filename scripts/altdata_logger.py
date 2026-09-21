#!/usr/bin/env python3
"""ALT-DATA LOGGER — accumulate the columns the APIs only keep for 30 days.

THE PROBLEM: Binance serves OI / long-short ratio / taker flow with only 30
days of history. That makes them impossible to walk-forward validate. Every
day we do not log is a day of history lost FOREVER.

THE FIX: log them daily. In a year we own 395 days of a dataset that no free
API will hand us. This is the cheapest way to buy backtestable data.

Also logs: Fear&Greed, stablecoin supply (liquidity), Deribit implied vol,
gold - the columns that may carry signal our price features do not.

Append-only: data/altdata.jsonl    One line per symbol per day.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
OUT = BASE / "data" / "altdata.jsonl"
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "NEARUSDT",
        "ARBUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT"]


def g(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers=H)
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except Exception:
        return None


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    recs = []

    # ---- market-wide ----
    fng = g("https://api.alternative.me/fng/?limit=1")
    fng_val = int(fng["data"][0]["value"]) if fng and fng.get("data") else None

    stables = g("https://stablecoins.llama.fi/stablecoins?includePrices=false")
    stable_mcap = None
    if stables and isinstance(stables, dict):
        tot = 0.0
        for s in stables.get("peggedAssets", []):
            if s.get("pegType") == "peggedUSD":
                tot += float(s.get("circulating", {}).get("peggedUSD", 0) or 0)
        stable_mcap = round(tot, 0) if tot else None

    dvol = g("https://www.deribit.com/api/v2/public/get_index_price"
             "?index_name=btc_usd")
    btc_index = (dvol["result"]["index_price"] if dvol and dvol.get("result")
                 else None)

    # ---- per symbol ----
    for s in SYMS:
        row = {"date": today, "symbol": s, "ts": datetime.now(
            timezone.utc).isoformat()}
        r = g(f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
              f"?symbol={s}&period=1d&limit=1")
        if r:
            row["ls_ratio"] = float(r[-1]["longShortRatio"])
            row["long_pct"] = float(r[-1]["longAccount"]) * 100
        r = g(f"https://fapi.binance.com/futures/data/takerlongshortRatio"
              f"?symbol={s}&period=1d&limit=1")
        if r:
            row["taker_ratio"] = float(r[-1]["buySellRatio"])
        r = g(f"https://fapi.binance.com/futures/data/openInterestHist"
              f"?symbol={s}&period=1d&limit=1")
        if r:
            row["oi"] = float(r[-1]["sumOpenInterest"])
            row["oi_value"] = float(r[-1]["sumOpenInterestValue"])
        r = g(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={s}")
        if r:
            row["funding"] = float(r["lastFundingRate"])
            row["mark"] = float(r["markPrice"])
        row["fng"] = fng_val
        row["stable_mcap"] = stable_mcap
        row["btc_index"] = btc_index
        recs.append(row)

    # idempotent: a re-run on the same day must not double-log. If today's
    # row for a symbol already exists we SKIP it - the first capture stands.
    existing = set()
    if OUT.exists():
        for line in OUT.read_text().splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                    existing.add((r.get("date"), r.get("symbol")))
                except Exception:
                    pass
    fresh = [r for r in recs if (r["date"], r["symbol"]) not in existing]
    skipped = len(recs) - len(fresh)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a", encoding="utf-8") as fh:
        for r in fresh:
            fh.write(json.dumps(r) + "\n")

    have = sum(1 for r in fresh if "ls_ratio" in r)
    extra = f" ({skipped} already logged today, skipped)" if skipped else ""
    print(f"[{datetime.now(timezone.utc):%m-%d %H:%M}] altdata logged "
          f"{len(fresh)} rows ({have} with positioning) | F&G={fng_val} "
          f"| stable mcap=${stable_mcap/1e9:.1f}B{extra}" if stable_mcap
          else f"[{datetime.now(timezone.utc):%m-%d %H:%M}] altdata logged "
               f"{len(fresh)} rows{extra}")
    # unique days so far — the count that matters
    try:
        days = sorted({json.loads(l)["date"] for l in
                       OUT.read_text().splitlines() if l.strip()})
        print(f"  history accumulated: {len(days)} unique day(s) "
              f"({days[0]} -> {days[-1]})" if days else "  history: none")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"altdata logger error: {str(e)[:200]}")
