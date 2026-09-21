#!/usr/bin/env python3
"""WEEKLY AUTONOMY REPORT — is the live system still doing what we promised?

Runs divergence (live vs backtest) and attribution (where the P&L actually
comes from), then pings Telegram ONLY when something needs attention.
Silence = healthy, which is the correct default.
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path("/opt/trading-agent")
sys.path.insert(0, str(BASE / "scripts"))

import autonomy as aut          # noqa: E402


def main():
    tr = aut._live_trades()
    n = len(tr)

    if n == 0:
        print(f"[autonomy] no closed trades yet — nothing to report (n=0)")
        return

    # divergence, captured rather than printed
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        aut.cmd_divergence()
        aut.cmd_attribution()
    out = buf.getvalue()
    print(out)

    alert = None
    if n >= 30:
        wins = [t for t in tr if t.get("ret", 0) > 0]
        wr = len(wins) / n * 100
        if wr < 30:
            alert = f"⚠️ LIVE WR {wr:.0f}% (n={n}) — below backtest promise"
        elif n >= 100 and wr < 36:
            alert = f"⚠️ LIVE WR {wr:.0f}% over {n} trades — edge may be decaying"
    elif n >= 10:
        wins = [t for t in tr if t.get("ret", 0) > 0]
        wr = len(wins) / n * 100
        if wr < 25:
            alert = (f"⚠️ early warning: live WR {wr:.0f}% after {n} trades "
                     f"(sample still small)")

    if alert:
        print("\nALERT:", alert)
        try:
            from trading.tg.bot_ui import send_card
            send_card("signals", "🧭 Weekly autonomy check", alert +
                      f"\n\nclosed trades: {n}")
        except Exception as e:
            print("notify failed:", str(e)[:60])
    else:
        print(f"\nno alert (n={n}) — silence by design")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"autonomy report error: {str(e)[:200]}")
