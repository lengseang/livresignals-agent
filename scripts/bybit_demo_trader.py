#!/usr/bin/env python3
"""BYBIT DEMO TRADER — run the real strategy on ~$100k fake money.

Purpose: prove the strategies with REAL order fills (not my simulator).
Trades the validated trend signal (coin > SMA50 AND BTC > SMA200) on Bybit
demo linear perps, including GOLD (XAUUSDT).

Sizing: $1,000 notional per position, max 4 positions, tracked on a $1,000
virtual book so PnL percentages are readable and transferable.

State: data/bybit-demo-state.json
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
sys.path.insert(0, str(BASE / "scripts"))
from bybit_demo import private, public          # noqa: E402

try:
    from gold_macro import macro_signal          # noqa: E402
except Exception:                                # never let macro break trading
    def macro_signal(*_a, **_k):
        return {"signal": "UNKNOWN", "note": "macro module unavailable"}

try:
    from econ_calendar import risk_state as _event_risk_state
except Exception:                                # never let calendar break trading
    def _event_risk_state(*_a, **_k):
        return {"blackout": False, "event": None}

GOLD_SYM = "XAUTUSDT"     # gold via Tether Gold

STATE = BASE / "data" / "bybit-demo-state.json"
SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT",
        "DOGEUSDT", "AVAXUSDT", "NEARUSDT", "DOTUSDT", "ATOMUSDT", "LTCUSDT",
        "XAUTUSDT"]           # GOLD via Tether Gold (1 token = 1 oz physical
                              # gold). Works without the TradFi terms gate that
                              # blocks XAUUSDT on demo accounts.
BOOK = 1000.0        # virtual book for readable %
SIZE = 300.0         # notional per position (30% of book)
MAX_POS = 4
SMA_N = 50
HARD_STOP = 0.15


def klines(symbol, interval="D", limit=300):
    r = public("/v5/market/kline", {"category": "linear", "symbol": symbol,
                                    "interval": interval, "limit": str(limit)})
    lst = r.get("result", {}).get("list", [])
    if not lst:
        return []
    return [float(x[4]) for x in reversed(lst)]   # close, ascending


def sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


_INSTR = {}


def qty_step(symbol):
    """Cache each symbol's qtyStep (order sizes must be an exact multiple)."""
    if symbol not in _INSTR:
        r = public("/v5/market/instruments-info",
                   {"category": "linear", "symbol": symbol})
        lst = r.get("result", {}).get("list", [])
        _INSTR[symbol] = float(lst[0]["lotSizeFilter"]["qtyStep"]) if lst else 0.001
    return _INSTR[symbol]


def round_qty(q, step):
    import math
    if step <= 0:
        step = 0.001
    n = math.floor(q / step)
    return round(n * step, 10)


def notify(title, body, topic="signals"):
    try:
        sys.path.insert(0, str(BASE / "src"))
        from trading.tg.bot_ui import send_card
        send_card(topic, title, body)
    except Exception:
        pass


def load():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"positions": {}, "real": [], "log": [], "book": BOOK, "created": None}


def save(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2))


def reconcile(st, real, pos):
    """Sync local state with the ACTUAL exchange positions (self-healing).

    Prevents drift if an order is closed externally, partially filled, or a
    previous run crashed mid-way.
    """
    r = private("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
    live = {p["symbol"]: p for p in r.get("result", {}).get("list", [])
            if float(p.get("size") or 0) > 0}
    # 1) state says open, exchange says closed -> drop it
    for rp in list(real):
        if rp["symbol"] not in live:
            real.remove(rp)
            pos[rp["symbol"]] = {"long": False}
            st["log"].append({"symbol": rp["symbol"], "why": "reconciled-closed",
                              "ts": datetime.now(timezone.utc).isoformat()})
            print(f"  RECONCILE: {rp['symbol']} no longer open on exchange -> dropped")
    # 2) exchange has a position we're not tracking -> adopt it
    tracked = {rp["symbol"] for rp in real}
    for sym, p in live.items():
        if sym not in tracked and float(p.get("size") or 0) > 0:
            entry = float(p["avgPrice"] or 0)
            real.append({"symbol": sym, "qty": p["size"], "entry": entry,
                         "ts": datetime.now(timezone.utc).isoformat(),
                         "adopted": True})
            pos[sym] = {"long": True, "entry": entry}
            st["log"].append({"symbol": sym, "why": "reconciled-adopted",
                              "ts": datetime.now(timezone.utc).isoformat()})
            print(f"  RECONCILE: adopted untracked {sym} @ {entry}")
    return live


def main():
    st = load()
    pos = st.setdefault("positions", {})
    real = st.setdefault("real", [])
    if not st.get("created"):
        st["created"] = datetime.now(timezone.utc).isoformat()

    reconcile(st, real, pos)

    btc = klines("BTCUSDT")
    if len(btc) < 205:
        print("BTC data insufficient")
        return
    regime_ok = btc[-1] > sma(btc, 200)
    events = []          # crypto -> "signals" topic
    gold_events = []     # gold   -> "gold" topic

    # Macro "fuel" check — only needed when gold is in play.
    # 10y study: gold correlates -0.47 with real yields and -0.46 with the
    # dollar; requiring macro to be non-hostile gave the best risk-adjusted
    # return and halved the drawdown.
    macro = macro_signal()
    macro_block_gold = macro.get("signal") == "HEADWIND"
    if GOLD_SYM in [r["symbol"] for r in real] or GOLD_SYM in SYMS:
        print(f"  MACRO: {macro.get('signal')} ({macro.get('note')})")

    # Event risk — we can't predict an event's OUTCOME, but we know its TIME.
    # Around high-impact USD releases, spread and slippage spike, so stand down.
    er = _event_risk_state()
    event_blackout = bool(er.get("blackout"))
    if event_blackout:
        print(f"  EVENT RISK: BLACKOUT — {er.get('event')} in "
              f"{er.get('minutes_until')} min -> no new entries")
    elif er.get("next_iso"):
        print(f"  EVENT RISK: clear (next: {er.get('next')} @ {er.get('next_iso')[:16]})")

    # --- exits first ---
    for rp in list(real):
        s = rp["symbol"]
        cl = klines(s)
        if len(cl) < 60:
            continue
        px = cl[-1]
        ret = px / rp["entry"] - 1
        s50 = sma(cl, SMA_N)
        broke = (s50 and px < s50) or ret <= -HARD_STOP
        why = "HARD-STOP" if ret <= -HARD_STOP else "trend-broke"
        # gold additionally exits when the macro turns hostile (fuel gone)
        if s == GOLD_SYM and macro_block_gold:
            broke, why = True, "macro-headwind"
        if broke:
            body = {"category": "linear", "symbol": s, "side": "Sell",
                    "orderType": "Market", "qty": rp["qty"], "reduceOnly": True}
            r = private("/v5/order/create", method="POST", body=body)
            ok = r.get("retCode") == 0
            pnl = (px - rp["entry"]) * float(rp["qty"])
            st["book"] = st.get("book", BOOK) + pnl
            st["log"].append({"symbol": s, "entry": rp["entry"], "exit": px,
                              "ret": round(ret, 5), "pnl": round(pnl, 2),
                              "why": why,
                              "ok": ok, "ts": datetime.now(timezone.utc).isoformat()})
            real.remove(rp)
            pos[s] = {"long": False}
            _ev = gold_events if s == GOLD_SYM else events
            _ev.append(f"{'🔴' if ok else '⚠️'} SOLD {s} @ {px} "
                       f"({ret*100:+.1f}%, ${pnl:+.2f}) [{why}]")
            print(f"  EXIT {s} ret={ret*100:+.2f}% pnl=${pnl:+.2f} why={why} ok={ok}")

    # --- entries ---
    if regime_ok:
        if event_blackout:
            print("  NO ENTRIES: high-impact event imminent")
        cands = []
        for s in SYMS:
            cl = klines(s)
            if len(cl) < 60:
                continue
            s50 = sma(cl, SMA_N)
            if s50 and cl[-1] > s50 and not pos.get(s, {}).get("long"):
                # gold needs the macro to be non-hostile (not a headwind)
                if s == GOLD_SYM and macro_block_gold:
                    print(f"  SKIP {s}: macro HEADWIND")
                    continue
                mom = cl[-1] / cl[-31] - 1 if len(cl) > 31 else 0
                # gold gets a momentum boost when macro is a genuine tailwind
                if s == GOLD_SYM and macro.get("signal") == "TAILWIND":
                    mom += 0.5
                cands.append((mom, s, cl[-1]))
        cands.sort(reverse=True)
        for mom, s, px in cands:
            if len(real) >= MAX_POS:
                break
            if event_blackout:      # stand down around high-impact releases
                break
            step = qty_step(s)
            qty = round_qty(SIZE / px, step)
            if qty * px < 5:
                continue
            body = {"category": "linear", "symbol": s, "side": "Buy",
                    "orderType": "Market", "qty": str(qty)}
            r = private("/v5/order/create", method="POST", body=body)
            if r.get("retCode") == 0:
                real.append({"symbol": s, "qty": str(qty), "entry": px,
                             "oid": r.get("result", {}).get("orderId", ""),
                             "ts": datetime.now(timezone.utc).isoformat()})
                pos[s] = {"long": True, "entry": px}
                _ev = gold_events if s == GOLD_SYM else events
                _ev.append(f"🟢 BOUGHT {s} @ {px} (${qty*px:.0f}, mom {mom*100:+.1f}%)")
                print(f"  ENTRY {s} qty={qty} px={px} ok")
            else:
                _ev = gold_events if s == GOLD_SYM else events
                _ev.append(f"⚠️ {s} entry failed: {r.get('retMsg','')[:60]}")
                print(f"  ENTRY FAIL {s}: {r.get('retCode')} {r.get('retMsg','')[:70]}")

    save(st)
    ev_tag = "BLACKOUT" if event_blackout else "clear"
    print(f"[{datetime.now(timezone.utc):%m-%d %H:%M}] DEMO TRADER | regime="
          f"{'BULL' if regime_ok else 'BEAR'} | macro={macro.get('signal')} | "
          f"events={ev_tag} | open={len(real)} | book=${st.get('book', BOOK):.2f}")
    if events:
        notify("🧪 BYBIT DEMO (crypto)",
               f"<b>{'Bull' if regime_ok else 'Bear'} regime</b> · "
               f"macro <b>{macro.get('signal')}</b> · events <b>{ev_tag}</b>\n"
               + "\n".join(events) +
               f"\n\nbook: ${st.get('book', BOOK):.2f}",
               topic="signals")
    if gold_events:
        notify("🥇 GOLD DESK",
               f"macro <b>{macro.get('signal')}</b> · events <b>{ev_tag}</b>\n"
               + "\n".join(gold_events) +
               f"\n\nbook: ${st.get('book', BOOK):.2f}",
               topic="gold")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"demo trader error: {str(e)[:200]}")
