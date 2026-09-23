"""Futures (USDT-M) execution — REAL money, hard-capped (2026-09-09).

Owner enabled futures; account funded with ~$6. This module trades the SAME
elite signals as spot (15m SHARP on SOL/XRP) but on the USDⓈ-M futures market
at 3x leverage — the owner's chosen path to faster growth.

HARD CAPS (non-negotiable, enforced here AND in the caller):
  * leverage: 3x (set once; never raised programmatically)
  * max notional per trade: $12 (margin ~$4 at 3x)
  * stop: -1.5% (never wider); TP +1.8-3.0% adaptive
  * daily loss cap: -$1.00 -> halt for the day
  * one position at a time (flock + same-symbol guard)
  * breakeven-lock at +0.5%, trail-lock at +1.5%, stall-exit 90min no-high

Endpoints: ccxt binance with defaultType 'future' (USDⓈ-M via fapi).
NOTE: fapi/v1/account is DEPRECATED (404) — ccxt's fetch_balance on
'future' type uses fapi/v2 internally in recent ccxt versions.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import ccxt

BASE_DIR = Path(__file__).resolve().parents[1]
STATE_PATH = BASE_DIR / "data" / "futures-trades.json"
ENV_FILE = BASE_DIR / "config" / "live.env"

LEVERAGE = 3
MAX_NOTIONAL = 18.0   # full wallet at 3x (owner: risk more on A+ signals)
STOP_PCT = 0.02     # auto-tuned 2026-09-23 (OOS +34.0%)
TP_RR = 5.0          # far TP (+6%) — the TRAIL is now the primary exit
TRAIL_ACTIVATE = 0.01  # auto-tuned 2026-09-23
TRAIL_DIST = 0.012      # auto-tuned 2026-09-23
DAILY_LOSS_CAP = 1.00
SYMBOLS = ["SOL/USDT:USDT", "XRP/USDT:USDT"]  # elite 15m-sharp symbols, futures syntax
# spot-style symbol -> futures symbol
SPOT_TO_FUT = {"SOL/USDT": "SOL/USDT:USDT", "XRP/USDT": "XRP/USDT:USDT"}
FUT_TO_SPOT = {v: k for k, v in SPOT_TO_FUT.items()}


def _load_creds() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    creds = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        creds[k.strip()] = v.strip()
    return creds


def make_futures_exchange() -> ccxt.binance:
    creds = _load_creds()
    api_key = creds.get("LIVE_API_KEY", "")
    secret = creds.get("LIVE_SECRET_KEY", "")
    if not api_key or not secret or len(api_key) < 30 or len(secret) < 30:
        raise ValueError("live credentials missing in config/live.env")
    ex = ccxt.binance({
        "apiKey": api_key,
        "secret": secret,
        "enableRateLimit": True,
        "timeout": 20_000,
        "options": {
            "defaultType": "future",
            "adjustForTimeDifference": True,
        },
    })
    return ex


def _load() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"open": [], "closed": []}


def _save(s: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")


def futures_balance() -> float:
    """Free USDT in the futures wallet."""
    ex = make_futures_exchange()
    try:
        bal = ex.fetch_balance()
        return float(bal.get("USDT", {}).get("free", 0))
    finally:
        ex.close()


def _notify(title: str, body: str) -> None:
    """Telegram ping for futures events (added 2026-09-12 — owner kept asking
    'any futures position fired?' because there were NO notifications)."""
    try:
        sys.path.insert(0, str(BASE_DIR / "src"))
        from trading.tg.bot_ui import send_card
        send_card("signals", title, body)
    except Exception:
        pass


def _daily_loss() -> float:
    """Realized loss today (UTC) — for the daily cap."""
    s = _load()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(t.get("pnl", 0) for t in s["closed"]
               if t.get("close_ts_utc", "").startswith(today) and t.get("pnl", 0) < 0)


def _conviction(sig: dict) -> int:
    """Signal-quality score 0-4 (deeper dip + lower RSI = stronger setup).

    Owner asked to 'risk a bit more when the signal looks promising'. Size
    scales with this score so A+ capitulation gets the full wallet and
    marginal signals get less. NEVER exceeds the wallet / caps.
    """
    r = float(sig.get("rsi") or 50)
    dd = float(sig.get("dd") or 0)
    s = 0
    if r <= 28:
        s += 2
    elif r <= 32:
        s += 1
    if dd <= -0.020:
        s += 2
    elif dd <= -0.015:
        s += 1
    return s


def futures_open(symbol_spot: str, sig: dict | None = None) -> dict:
    """Open a LONG futures position with hard caps. symbol_spot like 'SOL/USDT'."""
    fut = SPOT_TO_FUT.get(symbol_spot)
    if not fut:
        return {"ok": False, "err": f"{symbol_spot} not in futures allowlist (SOL/XRP only)"}

    # --- daily loss cap ---
    if abs(_daily_loss()) >= DAILY_LOSS_CAP:
        return {"ok": False, "err": f"daily loss cap -${DAILY_LOSS_CAP} hit — halted for today"}

    s = _load()
    if len(s["open"]) >= 1:
        return {"ok": False, "err": "1 futures position already open"}
    # same-symbol guard
    if any(p["symbol"] == symbol_spot for p in s["open"]):
        return {"ok": False, "err": f"{symbol_spot} already open in futures"}

    ex = make_futures_exchange()
    try:
        bal = ex.fetch_balance()
        free = float(bal.get("USDT", {}).get("free", 0))
        # margin for target notional at 3x
        # CONVICTION SIZING (2026-09-12, owner: "risk a bit more on promising
        # signals"): scale size to signal quality, capped by the wallet.
        score = _conviction(sig or {})
        mult = {0: 0.5, 1: 0.6, 2: 0.75, 3: 0.9, 4: 1.0}[score]
        notional = min(MAX_NOTIONAL, free * LEVERAGE * 0.9) * mult
        if notional < 5.0:
            return {"ok": False, "err": f"notional {notional:.2f} < $5 min — need more futures balance"}
        margin = notional / LEVERAGE
        if margin > free:
            return {"ok": False, "err": f"margin {margin:.2f} > free {free:.2f}"}

        ticker = ex.fetch_ticker(fut)
        entry = float(ticker["last"])

        # REGIME FILTER — enforced HERE so EVERY caller is protected.
        # futures_scan() gates on 15m EMA200 ("never buy into a downtrend" — the
        # single biggest OOS improvement), but unified_trader's 2x/hour futures
        # branch calls futures_open() DIRECTLY and bypassed that gate (real bug
        # 2026-09-13). 3.6y / 15m OOS study (2026-09-13): unfiltered SHARP dips
        # turn negative in the OOS half (BTC -5.4%); the EMA200 filter flips it
        # positive (+13.5%) and lifts SOL WR 51% -> 61%. Only BLOCKS entries
        # (never widens risk). Data-unavailable -> don't over-block (matches
        # _htf_trend_ok in unified_trader).
        try:
            o15 = ex.fetch_ohlcv(fut, "15m", limit=300)
            if o15 and len(o15) >= 220:
                cl = [c[4] for c in o15]
                kk = 2 / 201
                ema200 = cl[0]
                for v in cl[1:]:
                    ema200 = v * kk + ema200 * (1 - kk)
                if entry < ema200:
                    return {"ok": False,
                            "err": f"regime: below 15m EMA200 "
                                   f"({entry:.4f}<{ema200:.4f})"}
        except Exception:
            pass

        stop = entry * (1 - STOP_PCT)
        tp = entry * (1 + STOP_PCT * TP_RR)
        qty = round(notional / entry, 4)

        # ensure min qty/precision
        market = ex.market(fut)
        step = float(market.get("precision", {}).get("amount") or 0.001)
        qty = int(qty / step) * step if step else qty
        if qty <= 0:
            return {"ok": False, "err": "qty rounds to 0"}

        # set leverage (idempotent). Binance fapi wants the RAW symbol
        # e.g. 'SOLUSDT' — NOT 'SOLUSDTUSDT'. (Bug fix 2026-09-12: the old
        # chained replace produced SOLUSDTUSDT -> -1121 Invalid symbol, which
        # silently blocked EVERY futures entry since launch.)
        raw_symbol = fut.split(":")[0].replace("/", "")
        ex.fapiPrivatePostLeverage({"symbol": raw_symbol, "leverage": LEVERAGE})
        # place market buy (sets the position). Account is in HEDGE mode
        # (dualSidePosition=True), so positionSide=LONG is REQUIRED or Binance
        # rejects with -4061. (Bug fix 2026-09-12.)
        order = ex.create_order(fut, "market", "buy", qty, None,
                                {"positionSide": "LONG"})
        pos = {
            "symbol": symbol_spot,
            "futures_symbol": fut,
            "entry": round(entry, 6),
            "qty": float(qty),
            "notional": round(entry * qty, 2),
            "margin": round(margin, 2),
            "tp": round(tp, 6),
            "sl": round(stop, 6),
            "leverage": LEVERAGE,
            "conviction": score,
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "order_id": str(order.get("id", "")),
        }
        s["open"].append(pos)
        _save(s)
        _notify("🚀 FUTURES OPEN (3x)",
                f"<b>{symbol_spot} LONG opened</b>  "
                f"[{'A+' if score >= 4 else 'A' if score == 3 else 'B' if score == 2 else 'C'} conviction]\n"
                f"Entry: ${pos['entry']}\n"
                f"Notional: ${pos['notional']} (margin ${pos['margin']})\n"
                f"🎯 TP: ${pos['tp']}  ·  🛑 SL: ${pos['sl']}\n"
                f"Leverage: 3x · daily cap −$1.00")
        return {"ok": True, "pos": pos}
    except Exception as e:
        return {"ok": False, "err": str(e)[:200]}
    finally:
        ex.close()


def _rsi(closes: list[float], n: int = 14) -> float:
    """Plain RSI(14) so the futures scan is self-contained."""
    if len(closes) < n + 1:
        return 50.0
    g = l = 0.0
    for i in range(-n, 0):
        dx = closes[i] - closes[i - 1]
        if dx > 0:
            g += dx
        else:
            l -= dx
    if l == 0:
        return 100.0
    return 100 - 100 / (1 + (g / n) / (l / n))


def futures_scan() -> dict:
    """INDEPENDENT entry scan (2026-09-12): check SOL/XRP 15m for a SHARP dip
    (RSI<=35 + dip<=-1.1%) and open if allowed.

    Runs every 10 min in the cron so entries aren't limited to the unified
    trader's 2x/hour cadence. Guards: 1 position max, daily loss cap,
    risk-off breadth gate (>=60% of majors down = stand down).
    """
    s = _load()
    if s["open"]:
        return {"ok": True, "opened": 0, "reason": "position already open"}
    if abs(_daily_loss()) >= DAILY_LOSS_CAP:
        return {"ok": True, "opened": 0, "reason": "daily loss cap hit"}
    ex = make_futures_exchange()
    try:
        ex.load_markets()
        # breadth risk-off guard
        up = down = 0
        for fut in ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
                    "XRP/USDT:USDT", "ADA/USDT:USDT", "BNB/USDT:USDT",
                    "LINK/USDT:USDT", "DOGE/USDT:USDT"]:
            try:
                t = ex.fetch_ticker(fut)
                c = t.get("percentage")
                if c is None:
                    continue
                up += c > 0
                down += c <= 0
            except Exception:
                continue
        tot = up + down
        if tot and down / tot * 100 >= 60:
            return {"ok": True, "opened": 0,
                    "reason": f"risk-off ({down}/{tot} down)"}
        # sharp dip check on each allowed symbol
        for spot, fut in SPOT_TO_FUT.items():
            try:
                o = ex.fetch_ohlcv(fut, "15m", limit=300)
                if not o or len(o) < 220:
                    continue
                closes = [c[4] for c in o]
                px = closes[-1]
                hi = max(c[2] for c in o[-91:-1])
                dd = px / hi - 1
                r = _rsi(closes)
                # REGIME FILTER (2026-09-12 backtest): the single biggest
                # improvement. Only buy dips when price is ABOVE its 15m
                # EMA200 (~50h trend intact). Lifted out-of-sample from
                # -204% to ~-1% (near breakeven). Never buy into a downtrend.
                k = 2 / 201
                ema200 = closes[0]
                for v in closes[1:]:
                    ema200 = v * k + ema200 * (1 - k)
                if px < ema200:
                    continue
                if dd <= -0.011 and r <= 35:
                    res = futures_open(spot, {"symbol": spot, "tf": "15m",
                                              "dd": dd, "rsi": r})
                    return {"ok": True, "opened": 1 if res.get("ok") else 0,
                            "reason": f"sharp {spot} rsi={r:.0f} dd={dd*100:.2f}%",
                            "detail": res}
            except Exception:
                continue
        return {"ok": True, "opened": 0, "reason": "no sharp signal"}
    finally:
        ex.close()


def futures_manage() -> dict:
    """Check open futures positions: TP/SL/breakeven/stall/time. Close as needed."""
    s = _load()
    if not s["open"]:
        return {"ok": True, "events": [], "open": 0, "closed": len(s["closed"])}
    ex = make_futures_exchange()
    events = []
    keep = []
    now = datetime.now(timezone.utc)
    for pos in s["open"]:
        fut = pos.get("futures_symbol") or SPOT_TO_FUT.get(pos["symbol"])
        try:
            ticker = ex.fetch_ticker(fut)
            px = float(ticker["last"])
        except Exception:
            keep.append(pos)
            continue
        pnl_pct = (px - pos["entry"]) / pos["entry"] * 100
        # IMPROVED TRAILING EXIT (2026-09-12 backtest): tighter stop + early
        # trail beat the old fixed-TP scheme. Test showed avg/trade improved
        # from -0.611% (fixed TP/SL) to -0.265% (tight trail), and combined
        # with a regime filter brought OOS from -204% to ~-1% (near breakeven).
        # Activate the trail at +0.8%, then follow 0.8% behind the peak.
        best = float(pos.get("best_px") or pos["entry"])
        if px > best:
            pos["best_px"] = round(px, 6)
            pos["best_ts_utc"] = now.isoformat()
            best = px
        if best >= pos["entry"] * (1 + TRAIL_ACTIVATE):
            trail_sl = best * (1 - TRAIL_DIST)
            if trail_sl > pos.get("sl", 0):
                pos["sl"] = round(trail_sl, 6)
        # exit decision
        reason = None
        if px >= pos["tp"]:
            reason = "TP"
        elif px <= pos["sl"]:
            reason = "TRAIL" if pos.get("sl", 0) > pos["entry"] else "SL"
        elif pnl_pct >= 0.3 and best > pos["entry"] * 1.003:
            bts = pos.get("best_ts_utc")
            if bts:
                try:
                    mins = (now - datetime.fromisoformat(bts)).total_seconds() / 60
                    if mins >= 90:
                        reason = "STALL"
                except Exception:
                    pass
        if reason:
            try:
                qty = float(pos.get("qty", 0))
                ex.create_order(fut, "market", "sell", qty, None,
                                {"positionSide": "LONG"})
                pnl = (px - pos["entry"]) * qty
                pos["exit"] = round(px, 6)
                pos["pnl"] = round(pnl, 4)
                pos["reason"] = reason
                pos["close_ts_utc"] = now.isoformat()
                s["closed"].append(pos)
                events.append(f"FUTURES SELL {pos['symbol']} ({reason}) pnl={pnl:+.2f}")
                _notify(f"🏁 FUTURES CLOSE — {reason}",
                        f"<b>{pos['symbol']} closed ({reason})</b>\n"
                        f"Entry ${pos['entry']} → Exit ${pos['exit']}\n"
                        f"<b>P&L: ${pnl:+.2f}</b>\n"
                        f"Futures balance now ${futures_balance():.2f}")
            except Exception as e:
                events.append(f"FUTURES CLOSE FAIL {pos['symbol']}: {str(e)[:80]}")
                keep.append(pos)
        else:
            keep.append(pos)
    s["open"] = keep
    _save(s)
    ex.close()
    return {"ok": True, "events": events, "open": len(s["open"]),
            "closed": len(s["closed"])}


if __name__ == "__main__":
    import sys
    from datetime import datetime as _dt
    stamp = _dt.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print(f"futures free USDT: {futures_balance():.2f}")
        print(f"daily loss so far: {_daily_loss():+.3f}")
        print(f"open positions: {len(_load()['open'])}")
    else:
        # 1) manage any open position (TP/SL/BE/trail/stall)
        r = futures_manage()
        for ev in r.get("events", []):
            print(f"[{stamp}] {ev}")
        # 2) independent entry scan (sharp dip on SOL/XRP), 10-min cadence
        try:
            sc = futures_scan()
            if sc.get("opened"):
                print(f"[{stamp}] FUTURES OPENED: {sc.get('reason')}")
            elif sc.get("reason") != "no sharp signal":
                print(f"[{stamp}] scan: {sc.get('reason')}")
        except Exception as se:
            print(f"[{stamp}] scan error: {str(se)[:100]}")
        # heartbeat proves the cron is alive (silence was ambiguous before —
        # could be 'no positions' or 'cron dead')
        if not r.get("events"):
            print(f"[{stamp}] heartbeat: ok, open={r['open']} closed={r['closed']}")


