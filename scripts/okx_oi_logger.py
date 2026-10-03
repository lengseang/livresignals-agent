#!/usr/bin/env python3
"""Monitor-only OKX open-interest logger + squeeze flag.

WHY (2026-10-03): OKX's public oi-history endpoint gives ~2.75 years of daily OI
for free (see src/trading/signals/okx_oi.py). Binance caps OI at 30 days, which
is why our references call OI "MONITOR-ONLY, cannot be backtested". That cap is
venue-specific, not fundamental — so we now own a backtestable OI series.

This script does two things, NEITHER of which touches a trade:
  1. Appends a daily OKX-OI snapshot per symbol to data/okx-oi.jsonl
     (append-only; idempotent per (date, symbol) exactly like altdata_logger).
  2. Records the derived squeeze flag: True when BTC price ROSE over LOOKBACK
     days while BTC OI FELL (short-covering squeeze) — the veto condition from
     references/external-signals-and-tool-vetting.md §4.

⚠ MONITOR-ONLY. The backtest (research/oi/bt_oi_veto.py) found the squeeze flag
IS a robust loss-avoider at the ENTRY level (squeeze entries: n=91, 33% WR,
median −2.3%, negative in BOTH IS and OOS folds) but the underlying trend book
has no positive edge to protect (clean-entry median ≈ 0), so gating on it only
trades LESS, it does not make money. Do NOT wire this into the live entry path.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
OUT = BASE / "data" / "okx-oi.jsonl"
sys.path.insert(0, str(BASE / "src"))
from trading.signals.okx_oi import fetch_history, to_daily_series, oi_change_pct  # noqa: E402

# instId -> our Binance ticker (for cross-reference)
SYMS = {
    "BTC-USDT-SWAP": "BTCUSDT",
    "ETH-USDT-SWAP": "ETHUSDT",
    "SOL-USDT-SWAP": "SOLUSDT",
    "DOGE-USDT-SWAP": "DOGEUSDT",
    "NEAR-USDT-SWAP": "NEARUSDT",
    "ARB-USDT-SWAP": "ARBUSDT",
}
LOOKBACK = 3


def btc_price_up(lookback=LOOKBACK):
    """BTC daily closes from Binance; returns % change over `lookback` days."""
    import urllib.request
    u = ("https://api.binance.com/api/v3/klines?symbol=BTCUSDT"
         f"&interval=1d&limit={lookback + 1}")
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    k = json.loads(urllib.request.urlopen(req, timeout=15).read())
    closes = [float(r[4]) for r in k]
    if len(closes) < lookback + 1 or closes[-1 - lookback] == 0:
        return None
    return (closes[-1] / closes[-1 - lookback] - 1) * 100


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # idempotent per (date, symbol)
    existing = set()
    if OUT.exists():
        for line in OUT.read_text().splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                    existing.add((r.get("date"), r.get("symbol")))
                except Exception:
                    pass

    btc_px_chg = btc_price_up()
    recs = []
    for inst, sym in SYMS.items():
        if (today, sym) in existing:
            continue
        try:
            rows = fetch_history(inst, "1D", pages=8)
            dates, oi = to_daily_series(rows)
        except Exception as e:
            print(f"{sym}: fetch failed {str(e)[:60]}")
            continue
        if not oi:
            continue
        rec = {"date": today, "symbol": sym,
               "ts": datetime.now(timezone.utc).isoformat(),
               "oi_usd": round(oi[-1], 2),
               "oi_chg_pct": round(oi_change_pct(oi, len(oi) - 1, LOOKBACK) or 0.0, 4),
               "oi_bars": len(oi)}
        recs.append(rec)

    if recs:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("a", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")

    # squeeze flag = BTC price UP while BTC OI DOWN
    sq = None
    btc = next((r for r in recs if r["symbol"] == "BTCUSDT"), None)
    if btc and btc_px_chg is not None:
        sq = bool(btc_px_chg > 0 and btc["oi_chg_pct"] < 0)
    print(f"[{datetime.now(timezone.utc):%m-%d %H:%M}] okx-oi logged {len(recs)} "
          f"rows | btc_px_chg={btc_px_chg if btc_px_chg is None else round(btc_px_chg,2)}% "
          f"| squeeze={sq}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"okx-oi logger error: {str(e)[:200]}")
