"""Human-readable formatting for Telegram cards.

WHY THIS EXISTS
The original cards were raw field dumps in <pre> monospace:

    action     : BUY
    entry_ts   : 2026-09-21T04:20:02+00:00
    unrealized_pnl_pct : +55.3

That is the machine's view, not a person's. On a phone it reads as noise: the
labels are engineer-speak, timestamps are ISO strings, and nothing tells you
whether the news is good.

DESIGN RULES (keep these when extending):
  1. Lead with the CONCLUSION, not the fields. "Bought BTC" before "qty".
  2. Plain words. "Bought"/"Sold", not "ENTRY"/"CLOSE". "You made", not "pnl".
  3. Money in dollars. A percentage alone doesn't tell you what you earned.
  4. Human time. "4 hours ago", never an ISO timestamp.
  5. One idea per line, short lines. <pre> tables get clipped on a phone.
  6. Colour the news: 🟢 good / 🔴 bad / 🟡 neutral / ⚪ closed. The emoji is the
     fastest signal on a small screen.
  7. Never print a bare "?" — say "unknown" or omit the line.
  8. Escape everything interpolated (symbols, reasons) for HTML parse mode.
"""
from __future__ import annotations

import datetime as dt
import html

# ---------------------------------------------------------------- primitives


def esc(s) -> str:
    """Escape for Telegram HTML parse mode."""
    return html.escape(str(s), quote=False)


def money(x, cur: str = "$") -> str:
    """1234.5 -> '$1,234.50' ; 0.0000112 -> '$0.0000112'.

    Sub-cent coins are common here (PEPE trades near $0.00001), so the decimal
    count scales with magnitude. A fixed 4dp collapsed PEPE to a literal '$0',
    which is worse than useless on a trade alert.
    """
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    a = abs(v)
    if a == 0:
        return f"{cur}0.00"
    if a >= 1:
        return f"{cur}{v:,.2f}"
    if a >= 0.0001:
        s = f"{v:.4f}"
    elif a >= 0.000001:
        s = f"{v:.8f}"
    elif a >= 1e-12:
        s = f"{v:.12f}"
    else:
        return f"{cur}{v:.2e}"
    s = s.rstrip("0").rstrip(".")
    return f"{cur}{s}"


def signed_money(x, cur: str = "$") -> str:
    """Always shows the sign: '+$6.87' / '-$0.12'."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    sign = "+" if v >= 0 else "−"
    return f"{sign}{money(abs(v), cur)}"


def pct(x, digits: int = 1, signed: bool = True) -> str:
    """0.553 -> '+55.3%'. Accepts either a fraction (0.553) or a percent (55.3)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    if abs(v) <= 1.0:
        v *= 100
    sign = "+" if v >= 0 else "−"
    body = f"{abs(v):.{digits}f}%"
    return f"{sign}{body}" if signed else body


def ago(ts) -> str:
    """ISO timestamp or datetime -> '4 hours ago'. Never shows an ISO string."""
    if not ts:
        return "unknown"
    try:
        if isinstance(ts, dt.datetime):
            t = ts
        else:
            t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        secs = (dt.datetime.now(dt.timezone.utc) - t).total_seconds()
    except Exception:  # noqa: BLE001
        return "unknown"
    if secs < 0:
        return "in the future?"
    for limit, div, unit in ((90, 1, "second"), (5400, 60, "minute"),
                             (172800, 3600, "hour"), (5184000, 86400, "day")):
        if secs < limit:
            n = max(1, int(secs / div))
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    n = int(secs / 2592000)
    return f"{n} month{'s' if n != 1 else ''} ago"


def trend_emoji(v) -> str:
    """The fastest signal on a phone screen."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "⚪"
    if f > 0.001:
        return "🟢"
    if f < -0.001:
        return "🔴"
    return "🟡"


def bar(v, width: int = 10) -> str:
    """A tiny progress bar for ratio-ish values."""
    try:
        f = max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return ""
    n = round(f * width)
    return "▰" * n + "▱" * (width - n)


def dur(hours) -> str:
    """3.5 -> '3.5 hours' ; 52 -> '2.2 days'."""
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return "unknown"
    if h < 1:
        return f"{int(h * 60)} min"
    if h < 48:
        return f"{h:.1f} hours"
    return f"{h / 24:.1f} days"


# ------------------------------------------------------------------- cards


def entry_card(sym: str, price, usdt, qty, why: str, stop=None, target=None,
               when=None) -> str:
    """'You just bought this' — conclusion first, detail after."""
    L = [f"🟢 <b>Bought {esc(sym)}</b> at {money(price)}",
         f"💵 {money(usdt)} → {qty} {esc(str(sym).split('/')[0])}"]
    if why:
        L.append(f"📉 {esc(why)}")
    bits = []
    if stop:
        bits.append(f"🛑 stop {money(stop)}")
    if target:
        bits.append(f"🎯 target {money(target)}")
    if bits:
        L.append(" · ".join(bits))
    L.append(f"🕐 {ago(when)}")
    return "\n".join(L)


def exit_card(sym: str, ret, pnl_usd, why_plain: str, held_hours=None,
              entry=None, exit_px=None) -> str:
    """'You just sold this, here's why'."""
    e = trend_emoji(ret)
    L = [f"{e} <b>Sold {esc(sym)}</b> · {pct(ret)}"]
    if pnl_usd is not None:
        L.append(f"💵 {signed_money(pnl_usd)}")
    if entry and exit_px:
        L.append(f"{money(entry)} → {money(exit_px)}")
    if held_hours is not None:
        L.append(f"⏱ Held {dur(held_hours)}")
    if why_plain:
        L.append(f"<i>{esc(why_plain)}</i>")
    return "\n".join(L)


def pnl_card(cost, value, total_pnl, holdings: list[dict], note: str = "") -> str:
    """Portfolio snapshot. holdings = [{sym, pct, usd}] (already sorted)."""
    e = trend_emoji(total_pnl)
    L = [f"📊 <b>Your money: {money(value)}</b>",
         f"{e} {signed_money(total_pnl)} since you started "
         f"({pct(total_pnl / cost if cost else 0, signed=False)})"
         if cost else f"{e} {signed_money(total_pnl)}"]
    if holdings:
        L.append("")
        for h in holdings:
            L.append(f"{trend_emoji(h.get('pct'))} <b>{esc(h['sym'])}</b>"
                     f"{'   ' + pct(h['pct']) if h.get('pct') is not None else ''}"
                     f"{'   ' + signed_money(h['usd']) if h.get('usd') is not None else ''}")
    if note:
        L.append("")
        L.append(f"<i>{esc(note)}</i>")
    return "\n".join(L)


def status_card(vps: str, services: list[tuple], books: list[tuple],
                note: str = "") -> str:
    """Plain-English health. services/books = [(name, human_value, ok_bool)]."""
    L = ["🖥️ <b>System</b>", f"  {esc(vps)}"]
    if services:
        L.append("")
        for name, val, ok in services:
            L.append(f"{'🟢' if ok else '🔴'} {esc(name)} — {esc(val)}")
    if books:
        L.append("")
        L.append("📈 <b>Books</b>")
        for name, val, ok in books:
            L.append(f"{'🟢' if ok else '⚪'} {esc(name)} — {esc(val)}")
    if note:
        L.append("")
        L.append(f"<i>{esc(note)}</i>")
    return "\n".join(L)


# ------------------------------------------- plain-English reason mapping

REASONS = {
    "TIME": "didn't move in time — freed the slot",
    "TP": "hit the profit target",
    "SL": "hit the safety stop",
    "STALL": "stopped making new highs",
    "mom-decay": "momentum faded",
    "trend-broke": "the uptrend broke",
    "HARD-STOP": "hit the disaster stop",
    "TAKE PROFIT": "hit the profit target",
    "SOLD": "sold on signal",
    "CUT": "cut early",
}


def why_plain(reason: str, extra: str = "") -> str:
    """'TIME' -> 'didn't move in time — freed the slot'."""
    if not reason:
        return extra
    for k, v in REASONS.items():
        if k.lower() in str(reason).lower():
            return f"{v}{' — ' + extra if extra else ''}"
    return f"{reason}{' — ' + extra if extra else ''}"
