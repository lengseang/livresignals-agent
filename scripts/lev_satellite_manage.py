#!/usr/bin/env python3
"""Manage the bounded leverage satellite (defined-risk 3x trend position).

Exit rules (whichever hits first):
  1. TREND BREAK — SOL closes below its daily SMA50  -> close (limits loss)
  2. TAKE PROFIT — position up >= +25% on margin     -> close (locks gain)
  3. LIQUIDATION — automatic hard floor = the futures wallet (the $3 cap)

State: data/lev-satellite.json
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
sys.path.insert(0, str(BASE / "scripts"))
STATE = BASE / "data" / "lev-satellite.json"
FUT = "SOL/USDT:USDT"
SMA_N = 50
TP_PCT = 25.0     # take profit on margin


def notify(title, body):
    try:
        from trading.tg.bot_ui import send_card
        send_card("signals", title, body)
    except Exception:
        pass


def main():
    from futures_executor import make_futures_exchange

    if not STATE.exists():
        print("no satellite state — nothing to manage")
        return
    st = json.loads(STATE.read_text())
    if st.get("closed"):
        print("satellite already closed")
        return

    fc = make_futures_exchange()
    fc.load_markets()
    pos = [p for p in fc.fetch_positions() if p.get("contracts")]
    if not pos:
        st["closed"] = True
        st["reason"] = "position gone (closed or liquidated)"
        STATE.write_text(json.dumps(st, indent=2))
        notify("🛰️ LEV SATELLITE CLOSED",
               "Position no longer exists on the exchange (closed or liquidated).")
        print("position gone")
        return

    p = pos[0]
    entry = float(p["entryPrice"])
    mark = float(p["markPrice"])
    pnl_pct = (mark / entry - 1) * 100
    lev_pnl = pnl_pct * 3          # approx on margin

    o = fc.fetch_ohlcv(FUT, "1d", limit=120)
    closes = [c[4] for c in o]
    sma50 = sum(closes[-SMA_N:]) / SMA_N

    reason = None
    if lev_pnl >= TP_PCT:
        reason = f"TAKE PROFIT (+{lev_pnl:.1f}% on margin)"
    elif closes[-1] < sma50:
        reason = f"TREND BREAK (SOL {closes[-1]:.2f} < SMA50 {sma50:.2f})"

    if reason:
        fc.create_order(FUT, "market", "sell", float(p["contracts"]), None,
                        {"positionSide": "LONG"})
        st["closed"] = True
        st["reason"] = reason
        st["exit_px"] = mark
        st["exit_ts"] = datetime.now(timezone.utc).isoformat()
        STATE.write_text(json.dumps(st, indent=2))
        notify("🛰️ LEV SATELLITE CLOSED", f"<b>{reason}</b>\n"
               f"SOL {entry:.2f} → {mark:.2f} ({pnl_pct:+.1f}%, ~{lev_pnl:+.1f}% on margin)")
        print(f"CLOSED: {reason}")
    else:
        print(f"[{datetime.now(timezone.utc):%m-%d %H:%M}] satellite alive | "
              f"SOL {entry:.2f}->{mark:.2f} ({pnl_pct:+.1f}%, ~{lev_pnl:+.1f}% margin) | "
              f"SMA50 {sma50:.2f}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"satellite manage error: {str(e)[:150]}")
