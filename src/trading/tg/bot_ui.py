"""Telegram bot UI layer — rich cards, inline buttons, command menu.

Talk to Bot B (HERTRADE / leng_trader_bot) directly via the Bot API so we
control formatting + reply_markup (buttons), which `hermes send` cannot do.

Key capabilities:
- HTML-formatted message cards (monospace blocks, headers, emoji)
- Inline keyboard buttons (TradingView chart links, refresh actions, URLs)
- Bot command menu (/signals /pnl /status ...) via setMyCommands
- Topic-aware delivery (thread_id from config/topic-routing.json)

Token read from HERMES_HOME/.env (never logged).
"""
from __future__ import annotations

import importlib.util
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

ROUTING_REL = Path("config/topic-routing.json")
ENV_REL = Path("home/hermes-home/.env")  # HERMES_HOME/.env ; resolved below


def _env_path() -> Path:
    hh = Path.home() / "hermes-home" / ".env"
    if hh.exists():
        return hh
    return Path("/home/ubuntu/hermes-home/.env")


def _token() -> str:
    env = _env_path().read_text(encoding="utf-8")
    m = re.search("TELEGRAM_BOT" + "_TOKEN" + "=(\S+)", env)
    if not m:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not found in env")
    return m.group(1)


def _routing() -> dict:
    # Try cwd-first, then absolute VPS path, then repo config.
    for cand in (
        Path("config/topic-routing.json"),
        Path("/opt/trading-agent/config/topic-routing.json"),
        Path.cwd() / "config" / "topic-routing.json",
    ):
        if cand.exists():
            return json.loads(cand.read_text(encoding="utf-8"))
    raise RuntimeError("topic-routing.json not found")


def _api(method: str, payload: dict) -> dict:
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{_token()}/{method}", data=data
    )
    try:
        return json.load(urllib.request.urlopen(req, timeout=20))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _urls_button(symbol: str) -> list[dict]:
    """TradingView chart link button for a symbol."""
    tv = (
        "https://www.tradingview.com/chart/?symbol=BINANCE:"
        + symbol.replace("/", "")
    )
    return [{"text": "📈 Chart", "url": tv}]


def send_card(
    topic: str,
    title: str,
    body: str,
    buttons: list[list[dict]] | None = None,
    parse_mode: str = "HTML",
) -> dict:
    """Send a rich card to a topic with optional inline buttons."""
    routing = _routing()
    group = routing["group"]["id"]
    thread = routing["topics"][topic]
    markup = None
    if buttons:
        markup = json.dumps({"inline_keyboard": buttons})
    payload = {
        "chat_id": group,
        "message_thread_id": thread,
        "text": f"<b>{title}</b>\n{body}",
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true",
    }
    if markup:
        payload["reply_markup"] = markup
    return _api("sendMessage", payload)


def edit_card(message_id: int, chat_id: int, title: str, body: str) -> dict:
    """Edit a previously sent card (update cards without spam)."""
    return _api("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": f"<b>{title}</b>\n{body}",
        "parse_mode": "HTML",
    })


def set_command_menu(commands: list[tuple[str, str]]) -> dict:
    """Install the bot command menu (/signals /pnl ...)."""
    items = [{"command": c, "description": d} for c, d in commands]
    return _api("setMyCommands", {"commands": json.dumps(items)})


# ---- message builders ------------------------------------------------
# These render the HUMAN view: conclusion first, plain words, dollars, human
# time. See trading/tg/human.py for the design rules. Keep the signatures
# stable — live trading scripts call these.


def _human():
    try:
        from trading.tg import human
        return human
    except Exception:  # noqa: BLE001
        return None


def signal_card(entry: dict) -> tuple[str, str, list[list[dict]]]:
    """Build a structured signal card from an entry record."""
    symbol = entry.get("symbol", "?")
    h = _human()
    if h is None:
        return f"📈 ENTRY {symbol}", f"<pre>{entry}</pre>", [_urls_button(symbol)]

    price = entry.get("price")
    usdt = entry.get("amount_usdt")
    qty = entry.get("qty", entry.get("qty_btc"))
    dd = entry.get("drawdown_pct")
    why = ""
    try:
        if dd is not None and abs(float(dd)) > 0.05:
            why = f"Bought after a {abs(float(dd)):.1f}% dip"
    except (TypeError, ValueError):
        pass
    # derive stop/target so the card states the risk, not just the entry
    stop = target = None
    try:
        p = float(price)
        if dd is not None:
            stop = p * 0.975
            target = p * 1.025
    except (TypeError, ValueError):
        pass

    body = h.entry_card(symbol, price, usdt, qty, why, stop=stop, target=target,
                        when=entry.get("ts_utc"))
    title = f"📈 Bought {symbol}"
    buttons = [_urls_button(symbol),
               [{"text": "💵 How am I doing?", "callback_data": "cpnl:pnl"}]]
    return title, body, buttons


def exit_card(rec: dict, pnl_usd=None, held_hours=None) -> tuple[str, str, list[list[dict]]]:
    """Build a closed-trade card. Reads the raw log record, renders it plainly."""
    h = _human()
    sym = rec.get("symbol", "?")
    ret = rec.get("ret", rec.get("pnl_pct"))
    if h is None:
        return f"CLOSED {sym}", f"<pre>{rec}</pre>", [_urls_button(sym)]
    body = h.exit_card(sym, ret, pnl_usd if pnl_usd is not None else rec.get("pnl"),
                       h.why_plain(rec.get("why", "")), held_hours=held_hours,
                       entry=rec.get("entry"), exit_px=rec.get("exit"))
    e = h.trend_emoji(ret)
    title = f"{e} Sold {sym}"
    return title, body, [_urls_button(sym)]


def drawdown_note(peak_pct, now_pct) -> str:
    """'Peaked +88% · now +80% — gave back a little' when relevant."""
    h = _human()
    if h is None or peak_pct is None or now_pct is None:
        return ""
    try:
        pk, nw = float(peak_pct), float(now_pct)
    except (TypeError, ValueError):
        return ""
    if pk <= nw or pk <= 0:
        return ""
    back = (pk - nw) / pk * 100
    if back < 10:
        return ""
    note = f"Peaked {h.pct(pk)} · now {h.pct(nw)}"
    if back > 50:
        return note + " — gave back most of it"
    return note + f" — gave back {back:.0f}% of the peak"


def pnl_card(report: dict) -> tuple[str, str, list[list[dict]]]:
    """Build a PnL snapshot card."""
    h = _human()
    p = report.get("portfolio", {})
    if h is None:
        return "💵 PORTFOLIO PnL", f"<pre>{report}</pre>", []

    holdings = []
    for sym, v in (report.get("symbols") or {}).items():
        holdings.append({"sym": sym.replace("/USDT", ""),
                         "pct": v.get("pnl_pct"),
                         "usd": v.get("pnl")})
    holdings.sort(key=lambda x: (x["pct"] is None, -(x["pct"] or 0)))

    cost = p.get("cost_basis")
    value = p.get("market_value")
    total = p.get("unrealized_pnl")
    note = ""
    # state the cost of trading in plain words rather than hiding it
    if cost:
        note = "Before fees — 0.1% per side not deducted"
    body = h.pnl_card(cost, value, total, holdings, note=note)
    buttons = [
        [{"text": "🔄 Refresh", "callback_data": "cpnl:refresh_pnl"}],
        [{"text": "🔍 Check the record",
          "url": "https://livresignals.lengseang.dev"}],
    ]
    return "💵 How you're doing", body, buttons


def status_card(status: dict) -> tuple[str, str, list[list[dict]]]:
    """Build the /status card."""
    h = _human()
    if h is None:
        return "🖥️ SYSTEM STATUS", f"<pre>{status}</pre>", []
    services = status.get("services") or []
    books = status.get("books") or []
    if not services and not books:
        # backward-compatible: accept the old flat dict
        services = [(k.replace("_", " ").title(), str(v), v not in ("down", "error"))
                    for k, v in status.items() if k != "note"]
    body = h.status_card(status.get("vps", "unknown"), services, books,
                         note=status.get("note", ""))
    return "🖥️ System status", body, [[{"text": "💵 How am I doing?",
                                        "callback_data": "pnl"}]]