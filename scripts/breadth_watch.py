"""Breadth-recovery watcher — pings when the market flips from risk-off back
to tradeable, so the owner knows the exact moment futures/spot can fire again.

State file remembers the last state to avoid spamming: only pings on a
TRANSITION from risk-off (>=60% down) to normal (<60% down).
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE = BASE / "data" / "breadth-state.json"
sys.path.insert(0, str(BASE.parent / "src"))

from trading.exchange.live_client import make_live_exchange  # noqa: E402
from trading.tg.bot_ui import send_card  # noqa: E402


def compute_breadth(ex):
    syms = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT",
            "BNB/USDT", "LINK/USDT", "DOGE/USDT", "AVAX/USDT", "NEAR/USDT",
            "SUI/USDT", "ARB/USDT", "OP/USDT", "JUP/USDT"]
    up = down = 0
    for s in syms:
        try:
            t = ex.fetch_ticker(s)
            chg = t.get("percentage")
            if chg is None:
                continue
            if chg > 0:
                up += 1
            else:
                down += 1
        except Exception:
            continue
    tot = up + down
    down_pct = down / tot * 100 if tot else 0
    return up, down, down_pct


def main():
    ex = make_live_exchange()
    up, down, down_pct = compute_breadth(ex)
    risk_off = down_pct >= 60.0
    now_state = "RISK_OFF" if risk_off else "NORMAL"

    last = None
    if STATE.exists():
        try:
            last = json.loads(STATE.read_text()).get("state")
        except Exception:
            last = None
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"state": now_state, "up": up, "down": down,
                                 "down_pct": round(down_pct, 1)}))

    print(f"BREADTH: {up}up/{down}down ({down_pct:.0f}% down) -> {now_state}")
    # ping ONLY on transition risk_off -> normal (market healing)
    if last == "RISK_OFF" and now_state == "NORMAL":
        send_card("alerts", "📈 MARKET RECOVERING",
                  f"<b>Breadth flipped back to NORMAL!</b>\n"
                  f"{up} up / {down} down ({down_pct:.0f}% down)\n\n"
                  f"✅ Futures + spot real-money gates can now fire on "
                  f"sharp signals (SOL/XRP 15m).\n"
                  f"Watch for trade pings.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"breadth watch error: {str(e)[:120]}")
