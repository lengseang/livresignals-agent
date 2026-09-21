#!/usr/bin/env python3
"""TREND FOLLOWER v2 — optimised, OOS-validated, REAL-capable.

OPTIMAL CONFIG (2026-09-12 grid search, 16 coins x 1000 daily bars, no lookahead):
    signal = close > SMA50(coin)  AND  BTC > SMA200(BTC)   [BTC regime filter]
    IS +20.2%  |  OOS +12.6%  |  OOS drawdown -12.6%  |  ~353 trades
    EVERY BTC-filtered variant was positive in BOTH halves -> robust, not luck.

Why it works: it rides uptrends and exits to CASH the moment the trend breaks,
so it side-steps the crashes that killed the intraday system. Slow by design
(a few trades/quarter/coin) — this is a investing-style edge, not scalping.

Real execution (TREND_REAL=1): max 2 concurrent spot positions, $6 each,
market orders, tracked in data/trend-state.json. Off by default.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
STATE = BASE / "data" / "trend-state.json"
# PAPER universe — everything the strategy studies and learns from.
# MATIC/USDT removed 2026-09-17: delisted in the POL migration, so it burned an
# API call and a silent `continue` every cycle while contributing nothing.
SYMS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT", "ADA/USDT",
        "LINK/USDT", "DOGE/USDT", "AVAX/USDT", "NEAR/USDT", "SUI/USDT", "ARB/USDT",
        "DOT/USDT", "LTC/USDT", "ATOM/USDT",
        # Expanded 2026-09-17: +38 liquid USDT pairs, each verified tradable with
        # >=605 daily bars (enough history to backtest AND to judge a promotion on).
        # ONDO and AERO were rejected for having <605 bars.
        "AAVE/USDT", "ALGO/USDT", "APT/USDT", "AR/USDT", "AVA/USDT", "BCH/USDT",
        "CAKE/USDT", "CRV/USDT", "DASH/USDT", "ENA/USDT", "ETHFI/USDT", "FET/USDT",
        "FIL/USDT", "GALA/USDT", "HBAR/USDT", "INJ/USDT", "IOTA/USDT", "ONE/USDT",
        "OP/USDT", "PENDLE/USDT", "PEPE/USDT", "POL/USDT", "RAY/USDT", "RUNE/USDT",
        "SAGA/USDT", "SAND/USDT", "SEI/USDT", "SHIB/USDT", "SNX/USDT", "TAO/USDT",
        "TIA/USDT", "TRUMP/USDT", "TRX/USDT", "UNI/USDT", "VET/USDT", "WLD/USDT",
        "XLM/USDT", "ZEN/USDT"]

# REAL-MONEY universe — a strict subset of SYMS. A coin earns its way in here by
# proving itself on the paper ledger (>=55% WR over 15+ trades), the same rule the
# symbol-tier gate in unified_trader.py uses. Widening SYMS therefore buys DATA,
# not risk: the extra coins are studied and never traded until they qualify.
# Promote with scripts/promotion_report.py.
REAL_SYMS = {"BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
             "ADA/USDT", "LINK/USDT", "DOGE/USDT", "AVAX/USDT", "NEAR/USDT",
             "SUI/USDT", "ARB/USDT", "DOT/USDT", "LTC/USDT", "ATOM/USDT"}
SMA_N = 50          # optimised (SMA50 beat 100/150/200 OOS)
MAX_POS = 3         # 3 positions (ARB/NEAR/DOGE) — capital fully deployed
SIZE_USD = 5.5      # per position
HARD_STOP = 0.15    # disaster stop: exit at -15% even if the trend hasn't broken
REAL = os.environ.get("TREND_REAL", "0") == "1"


def sma(v, n):
    return sum(v[-n:]) / n


def signal(coin_closes, btc_closes):
    """Long if coin above SMA50 AND BTC above SMA200 (regime filter)."""
    if len(coin_closes) < 60 or len(btc_closes) < 205:
        return False
    return (coin_closes[-1] > sma(coin_closes, SMA_N)
            and btc_closes[-1] > sma(btc_closes, 200))


def load():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"positions": {}, "log": [], "real": {"open": []}}


def save(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def notify(title, body):
    try:
        sys.path.insert(0, str(BASE / "src"))
        from trading.tg.bot_ui import send_card
        send_card("signals", title, body)
    except Exception:
        pass


def _build_message(events, n_real, real_on) -> tuple[str, str]:
    """Turn raw events into a human-readable (title, body).

    Split out of main() so it can be tested without touching the exchange.

    Rules: separate REAL money from paper; say 'Bought'/'Sold' not BUY/SELL;
    state the reason in words, not an enum; never mix an error into the same
    list as a normal action.
    """
    try:
        from trading.tg import human as H
    except Exception:  # noqa: BLE001
        H = None

    real_lines, paper_lines, err_lines = [], [], []
    for kind, sym, px, extra in events:
        if kind == "REALBUY":
            real_lines.append(
                (f"🟢 <b>Bought {H.esc(sym)}</b> at {H.money(px)}"
                 f" — spent {H.money(extra.get('notional'))}")
                if H else f"REAL BUY {sym} @ {px}")
        elif kind == "OPEN":
            paper_lines.append(
                (f"🟢 Bought {H.esc(sym)} at {H.money(px)}")
                if H else f"OPEN {sym} @ {px}")
        elif kind == "EXIT":
            ret = extra.get("ret")
            if H:
                line = (f"{H.trend_emoji(ret)} <b>Sold {H.esc(sym)}</b>"
                        f" at {H.money(px)}"
                        + (f" · {H.pct(ret)}" if ret is not None else "")
                        + f"\n     <i>{H.esc(H.why_plain(extra.get('why', '')))}</i>")
            else:
                line = f"SELL {sym} @ {px} ({extra.get('why')})"
            (real_lines if extra.get("real") else paper_lines).append(line)
        elif kind == "ERR":
            err_lines.append((f"⚠️ {H.esc(sym)}: {H.esc(extra)}") if H
                             else f"{sym}: {extra}")

    blocks = []
    if real_lines:
        blocks.append("💰 <b>REAL MONEY</b>\n" + "\n".join(real_lines))
    if paper_lines:
        blocks.append("📄 <b>Paper</b>\n" + "\n".join(paper_lines))
    if err_lines:
        blocks.append("⚠️ <b>Needs attention</b>\n" + "\n".join(err_lines))

    n_change = len([e for e in events if e[0] != "ERR"])
    title = ((f"📈 Trend follower · {n_change} change"
              f"{'s' if n_change != 1 else ''}") if n_change
             else "📈 Trend follower · needs attention")
    foot = ("<i>Buys coins in an uptrend, sells when the trend breaks. "
            f"Holding {n_real} real position{'s' if n_real != 1 else ''}"
            + ("" if real_on else " · real trading OFF") + "</i>")
    return title, "\n\n".join(blocks) + f"\n\n{foot}"


def main():
    sys.path.insert(0, str(BASE / "scripts"))
    sys.path.insert(0, str(BASE / "src"))   # required: `trading` package lives in src/
    from trading.exchange.live_client import make_live_exchange
    ex = make_live_exchange()
    st = load()
    pos = st.setdefault("positions", {})
    real = st.setdefault("real", {"open": []})
    events = []

    btc = None
    try:
        o = ex.fetch_ohlcv("BTC/USDT", "1d", limit=300)
        if o:
            btc = [c[4] for c in o]
    except Exception:
        pass
    if not btc:
        print("no BTC data — abort")
        return

    free_usdt = 0.0
    if REAL:
        try:
            bal = ex.fetch_balance()
            free_usdt = float(bal.get("USDT", {}).get("free", 0))
        except Exception:
            pass

    candidates = []   # (momentum, symbol, px) for real ranking
    for s in SYMS:
        try:
            o = ex.fetch_ohlcv(s, "1d", limit=300)
            if not o or len(o) < 60:
                continue
            closes = [c[4] for c in o]
            px = closes[-1]
            want = signal(closes, btc)
            held = pos.get(s, {}).get("long", False)

            ent_held = pos.get(s, {}).get("entry", px) or px
            hard_hit = held and (px / ent_held - 1) <= -HARD_STOP

            if want and not held:
                pos[s] = {"long": True, "entry": px,
                          "ts": datetime.now(timezone.utc).isoformat()}
                events.append(("OPEN", s, px, None))
                mom = closes[-1] / closes[-31] - 1 if len(closes) > 31 else 0.0
                candidates.append((mom, s, px))
            elif (not want and held) or hard_hit:
                ent = ent_held
                ret = px / ent - 1
                why = "HARD-STOP" if hard_hit and want else \
                      ("HARD-STOP" if hard_hit else "trend-broke")
                st["log"].append({"symbol": s, "entry": ent, "exit": px,
                                  "ret": round(ret, 5), "why": why,
                                  "ts": datetime.now(timezone.utc).isoformat()})
                pos[s] = {"long": False, "entry": 0,
                          "ts": datetime.now(timezone.utc).isoformat()}
                # Tag whether this exit was REAL money, so the notification can
                # separate real actions from paper ones. Without it the owner sees
                # a real sell and a paper sell in the same undifferentiated list.
                was_real = any(rp["symbol"] == s
                               for rp in (real.get("open") or []))
                events.append(("EXIT", s, px,
                               {"ret": ret, "why": why, "real": was_real}))
                if REAL:
                    for rp in list(real["open"]):
                        if rp["symbol"] == s:
                            try:
                                # Sell only what is actually there. The recorded qty
                                # drifts: DOGE was booked at 58 while the exchange held
                                # 23 after an unrecorded partial sell on 09-13, so the
                                # old create_order(qty=rp["qty"]) would have been
                                # rejected forever — stranding the position with no
                                # retry and no reconciliation against the exchange.
                                base = s.split("/")[0]
                                bal = ex.fetch_balance()
                                free = float(bal.get(base, {}).get("free", 0) or 0)
                                qty = min(float(rp["qty"]), free)
                                mmin = float(ex.market(s).get("limits", {})
                                             .get("amount", {}).get("min") or 0)
                                if qty <= 0 or qty < mmin:
                                    real["open"].remove(rp)
                                    events.append(("ERR", s, px,
                                                   f"no sellable balance "
                                                   f"(free={free:g}, booked="
                                                   f"{rp['qty']:g}) — record dropped"))
                                    continue
                                ex.create_order(s, "market", "sell", qty)
                                real["open"].remove(rp)
                            except Exception as e:
                                events.append(("ERR", s, px, str(e)[:60]))
        except Exception:
            continue

    # REAL EXECUTION — open the top MAX_POS candidates ranked by 30d momentum,
    # but ONLY from REAL_SYMS. `candidates` is drawn from the full paper universe,
    # so without this gate widening SYMS would quietly widen real-money exposure —
    # which is the exact opposite of the intent.
    if REAL and candidates:
        candidates.sort(reverse=True)
        for mom, s, px in candidates:
            if s not in REAL_SYMS:
                continue                      # paper-only until promoted
            if len(real["open"]) >= MAX_POS:
                break
            if free_usdt < 5.01:          # need the $5 min notional
                break
            try:
                mkt = ex.market(s)
                step = float(mkt.get("precision", {}).get("amount") or 0.0001)
                use = min(SIZE_USD, free_usdt * 0.999)
                qty = int(use / px / step) * step
                if qty * px < 5.0:        # bump one step to clear Binance's min
                    qty = round(qty + step, 10)
                notional = qty * px
                if notional < 5.0 or notional > free_usdt:
                    continue
                od = ex.create_order(s, "market", "buy", qty)
                real["open"].append({"symbol": s, "qty": qty, "entry": px,
                                     "oid": str(od.get("id", "")),
                                     "ts": datetime.now(timezone.utc).isoformat()})
                free_usdt -= notional
                events.append(("REALBUY", s, px, {"qty": qty, "notional": round(notional, 2)}))
            except Exception as e:
                events.append(("ERR", s, px, str(e)[:60]))

    save(st)

    if events:
        title, body = _build_message(events, len(real.get("open") or []), REAL)
        notify(title, body)

    held = [k for k, v in pos.items() if v.get("long")]
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M}] TREND v2 "
          f"(SMA{SMA_N}+BTCfilter) | long {len(held)}/{len(SYMS)}: {held}")
    print(f"  real positions: {len(real['open'])}/{MAX_POS} | REAL={'ON' if REAL else 'off'}")
    for kind, sym, px, extra in events:
        print(f"  {kind} {sym} @ {px}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"trend follower error: {str(e)[:150]}")
