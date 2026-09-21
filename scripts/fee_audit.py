#!/usr/bin/env python3
"""Fee audit — what the exchange ACTUALLY charges, and what that does to the books.

WHY THIS EXISTS (2026-09-21)
Every performance number this system produced was GROSS. `ret` is computed from
entry/exit prices only; no book carried a fee field. The live book read dead-flat
(0.00%/trade) while genuinely losing to commissions. The website correctly says
"costs and slippage are NOT included" — but that is a disclosure standing in for
a fact we can simply measure.

Binance reports a `fee` on every fill. Two traps:
  1. Fees are charged in EITHER USDT or BNB (the BNB discount path). Summing
     across currencies without converting is meaningless.
  2. BNB fees are converted at TODAY's BNB price. For fees paid weeks ago this is
     an approximation of current value, not the historical cost.

Run: cron daily, or `python fee_audit.py`
Writes: data/fee-audit.json
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DATA = ROOT / "data"
OUT = DATA / "fee-audit.json"

# real-money universe; paper coins never touch the exchange
SYMS = ["ARB/USDT", "NEAR/USDT", "DOGE/USDT", "SOL/USDT", "LINK/USDT",
        "XRP/USDT", "ADA/USDT", "SUI/USDT", "AVAX/USDT", "ATOM/USDT",
        "SAND/USDT", "ETH/USDT", "BTC/USDT", "BNB/USDT", "TON/USDT",
        "TRX/USDT"]


def main() -> int:
    from trading.exchange.live_client import make_live_exchange
    ex = make_live_exchange()

    try:
        bnb_px = float(ex.fetch_ticker("BNB/USDT")["last"])
        bnb_ok = True
    except Exception as e:  # noqa: BLE001
        bnb_px, bnb_ok = 0.0, False
        print(f"WARN: BNB price unavailable ({str(e)[:50]}) — BNB fees unconverted")

    fee_usdt: dict[str, float] = defaultdict(float)
    fee_bnb: dict[str, float] = defaultdict(float)
    vol: dict[str, float] = defaultdict(float)
    nfills: dict[str, int] = defaultdict(int)
    per_trade_rates: list[float] = []

    for sym in SYMS:
        try:
            trades = ex.fetch_my_trades(sym, limit=200)
        except Exception as e:  # noqa: BLE001
            print(f"  {sym}: {type(e).__name__}: {str(e)[:60]}")
            continue
        for t in trades:
            fee = t.get("fee") or {}
            cost = float(fee.get("cost") or 0)
            cur = (fee.get("currency") or "").upper()
            notional = abs(float(t.get("cost") or 0)) or 0.0
            nfills[sym] += 1
            vol[sym] += notional
            if cur == "BNB":
                fee_bnb[sym] += cost
            else:
                fee_usdt[sym] += cost
            if notional > 1.0 and cost > 0:
                usd = cost * bnb_px if cur == "BNB" else cost
                per_trade_rates.append(usd / notional)

    rows = []
    for sym in set(list(fee_usdt) + list(fee_bnb)):
        fu, fb = fee_usdt[sym], fee_bnb[sym]
        fbv = fb * bnb_px
        rows.append({"symbol": sym, "fills": nfills[sym], "volume_usd": round(vol[sym], 4),
                     "fee_usdt": round(fu, 8), "fee_bnb": round(fb, 10),
                     "fee_bnb_usd": round(fbv, 6), "fee_total_usd": round(fu + fbv, 6)})
    rows.sort(key=lambda r: -r["fee_total_usd"])

    tot_fee = sum(r["fee_total_usd"] for r in rows)
    tot_vol = sum(r["volume_usd"] for r in rows)
    n_fills = sum(r["fills"] for r in rows)
    rate = (tot_fee / tot_vol) if tot_vol else 0.0

    # measured per-side rate, used to charge fees against the books
    per_side = (sum(per_trade_rates) / len(per_trade_rates)) if per_trade_rates else 0.001

    # apply to the trend book (the one holding real money)
    applied = {}
    try:
        trend = json.loads((DATA / "trend-state.json").read_text())
        log = trend.get("log", [])
        if log:
            stake = 5.0
            gross_pct = sum(t.get("ret", 0) for t in log) / len(log)
            gross_usd = sum(t.get("ret", 0) for t in log) * stake
            fee_usd = len(log) * stake * per_side * 2      # both sides
            applied["trend_closed"] = {
                "trades": len(log),
                "gross_avg_pct": round(gross_pct * 100, 3),
                "net_avg_pct": round(gross_pct * 100 - per_side * 200, 3),
                "gross_usd": round(gross_usd, 4),
                "fees_usd": round(fee_usd, 4),
                "net_usd": round(gross_usd - fee_usd, 4),
            }
    except Exception as e:  # noqa: BLE001
        print(f"  trend book: {e}")

    doc = {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "bnb_price_usd": round(bnb_px, 2) if bnb_ok else None,
        "totals": {
            "fills": n_fills,
            "volume_usd": round(tot_vol, 4),
            "fees_usd": round(tot_fee, 6),
            "effective_rate_per_fill": round(rate, 8),
            "effective_rate_pct_per_fill": round(rate * 100, 5),
            "measured_round_trip_pct": round(rate * 200, 5),
        },
        "per_symbol": rows,
        "applied_to_books": applied,
        "caveats": [
            "Fills are all-time per symbol and may include retired books.",
            "BNB-denominated fees are converted at the CURRENT BNB price.",
            "This measures REAL trading only; paper books pay no fees by design.",
        ],
    }
    OUT.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    print(f"fee audit written: {OUT}")
    print(f"  fills            : {n_fills}")
    print(f"  volume           : ${tot_vol:,.2f}")
    print(f"  fees paid        : ${tot_fee:.4f}")
    print(f"  MEASURED rate    : {rate*100:.4f}% per fill  "
          f"({rate*200:.4f}% round trip)")
    print(f"  vs assumed 0.1%/side -> "
          f"{'LOWER (assumption was safe)' if rate < 0.001 else 'HIGHER (assumption too kind!)'}")
    for k, v in applied.items():
        print(f"  {k}: gross {v['gross_avg_pct']:+.2f}%/trade -> "
              f"net {v['net_avg_pct']:+.2f}%/trade  "
              f"(${v['gross_usd']:+.2f} -> ${v['net_usd']:+.2f})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"fee audit failed: {type(e).__name__}: {e}")
        raise SystemExit(1)
