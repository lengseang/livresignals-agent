#!/usr/bin/env python3
"""Live auto-trader executor — REAL money, full TP/SL lifecycle.

The missing link between the adaptive engine (which decides WHEN) and the live
Binance client (which executes). This module handles:

  - open_long(): real market BUY, sized in USDT, with computed TP/SL
  - manage(): check live price vs TP/SL/time-stop, place real SELL when hit
  - track state in data/live-trades.json

SAFETY: only trades if config/live.env has valid creds AND a LIVE_ENABLED
flag is set. Without explicit enablement, every function returns {"ok": False,
"reason": "live disabled"} — it never silently falls back to paper.

Paper-vs-live is decided UPSTREAM (the engine), never guessed here.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading.exchange.live_client import (  # noqa: E402
    live_balances, make_live_exchange, place_market_buy, place_market_sell,
)

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "data" / "live-trades.json"
ENABLE_FLAG = ROOT / "config" / "LIVE_ENABLED"

# Pairs whose ~$6 order survives Binance lot-rounding to clear the $5 min
# notional. VERIFIED LIVE 2026-09-04 at $6 size (notional after rounding):
#   XRP $5.98  SOL $5.96  DOGE $6.00  ADA $5.99  LINK $5.94
#   AVAX $5.94 BNB $5.76  SUI $5.92  TON $6.00  TRX $5.97
ALLOWED_REAL = {"XRP/USDT", "SOL/USDT", "DOGE/USDT", "ADA/USDT", "LINK/USDT",
                "AVAX/USDT", "BNB/USDT", "SUI/USDT", "TON/USDT", "TRX/USDT"}

# Trade sizing + risk  (conservative small-account defaults; override via config/live-small-account.env)
try:
    from pathlib import Path as _P
    _cfg = (_P(__file__).resolve().parents[1] / "config" / "live-small-account.env")
    if _cfg.exists():
        ns = {}
        for _line in _cfg.read_text().splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                k, v = _line.split("=", 1)
                v = v.strip()
                if "#" in v:
                    v = v.split("#", 1)[0].strip()  # strip inline comment
                ns[k.strip()] = v
        DEFAULT_SIZE_USDT = float(ns.get("LIVE_SIZE_USDT", 5.0))
        MAX_SIZE_USDT = float(ns.get("LIVE_MAX_SIZE_USDT", 5.0))
        STOP_ATR_MULT = 1.5
        TP_RR = float(ns.get("LIVE_TP_RR", 2.0))
        HOLD_HOURS = float(ns.get("LIVE_HOLD_HOURS", 6))
        LIVE_STOP_PCT = float(ns.get("LIVE_STOP_PCT", 0.015))
        LIVE_MAX_OPEN = int(ns.get("LIVE_MAX_OPEN", 1))
        LIVE_MIN_FREE = float(ns.get("LIVE_MIN_FREE_BUFFER", 6.0))
        LIVE_MAX_DAILY_LOSS = float(ns.get("LIVE_MAX_DAILY_LOSS", 1.0))
        TRADE_ALL_SIGNALS = bool(int(ns.get("LIVE_TRADE_ALL_SIGNALS", 0)))
    else:
        DEFAULT_SIZE_USDT = 5.0
        MAX_SIZE_USDT = 10.0
        STOP_ATR_MULT = 1.5
        TP_RR = 2.0
        HOLD_HOURS = 6
        LIVE_STOP_PCT = 0.015
        LIVE_MAX_OPEN = 1
        LIVE_MIN_FREE = 6.0
        LIVE_MAX_DAILY_LOSS = 1.0
        TRADE_ALL_SIGNALS = False
except Exception:  # noqa: BLE001
    DEFAULT_SIZE_USDT = 5.0
    MAX_SIZE_USDT = 10.0
    STOP_ATR_MULT = 1.5
    TP_RR = 2.0
    HOLD_HOURS = 6
    LIVE_STOP_PCT = 0.015
    LIVE_MAX_OPEN = 1
    LIVE_MIN_FREE = 6.0
    LIVE_MAX_DAILY_LOSS = 1.0
    TRADE_ALL_SIGNALS = False


def live_enabled() -> bool:
    return ENABLE_FLAG.exists()


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


def _exit_params() -> dict:
    """Self-calibrated exit parameters (scripts/exit_calibrate.py, weekly cron).

    Measured 2026-09-19 on our own 30 closed trades: holding a median 4.0h while
    the peak arrives ~2-3h gave back ~100% of the move, and the -1.0% stop sat
    INSIDE the market's normal dip range (median dip -1.31%), so it fired on
    ordinary wiggle — 12 of 30 exits were SL against 2 TP.

    Falls back to the previous hard-coded behaviour whenever the file is missing,
    malformed, or out of sane bounds, so a failed calibration can never stop
    trading or silently widen risk without limit.
    """
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "data" / "exit-params.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        h = max(0.5, min(12.0, float(d.get("hold_hours", HOLD_HOURS))))
        s = max(0.005, min(0.10, float(d.get("disaster_stop_pct", LIVE_STOP_PCT))))
        return {"hold_hours": h, "stop_pct": s, "n": int(d.get("n", 0)),
                "as_of_utc": d.get("as_of_utc")}
    except Exception:  # noqa: BLE001  — never let calibration break trading
        return {"hold_hours": float(HOLD_HOURS), "stop_pct": float(LIVE_STOP_PCT),
                "n": 0, "as_of_utc": None}


def _adaptive_plan(sig: dict | None, entry: float) -> dict:
    """ADAPTIVE TP/SL/size from market context (owner request 2026-09-06).
    Uses volatility (ATR%) + momentum (ROC) + trend slope to scale exits:

      stop_pct = clamp(1.0 .. 2.5,  ATR% * 1.2)      — wild coin → wider stop
      tp_pct   = stop_pct * RR, where RR = 1.8 if weak momentum, 2.5 if strong
      size     = base * size_mult                    — calm+trending → bigger

    Fixed 1.5% stops were wrong in BOTH directions: too wide for calm chop
    (losses ran -0.84% vs wins +0.63%) and too tight for runners (TP missed by
    a hair in 1.40-1.43 XRP range). ATR scaling fits the stop to the coin's
    actual behavior; momentum scales the reward and conviction scales size.
    """
    base_size = DEFAULT_SIZE_USDT
    cal = _exit_params()
    stop_floor = max(0.010, cal["stop_pct"])     # never tighter than measured
    # ceiling: 4.0% when calibrated (so a wild coin still gets room), but the
    # original 2.5% when there is NO calibration, so an absent/failed calibration
    # reproduces the previous behaviour exactly rather than widening risk on its own.
    stop_ceil = max(stop_floor, 0.040 if cal["n"] else 0.025)
    if sig is None:
        # no context (manual/open call): use the calibrated stop
        stop_pct = stop_floor
        return {"size": base_size, "stop_pct": stop_pct,
                "tp": round(entry * (1 + stop_pct * TP_RR), 4),
                "sl": round(entry * (1 - stop_pct), 4)}

    atr_pct = float(sig.get("atr_pct", 0) or 0)
    mom = float(sig.get("mom", 0) or 0)
    trend = float(sig.get("trend", 0) or 0)

    # 1) STOP: ATR-scaled, FLOORED by the self-calibrated stop.
    # The old floor of 1.0% was the binding constraint, and it sat INSIDE the
    # market's normal dip range — our measured median intra-trade dip is -1.31%,
    # so a -1.0% stop was a dip detector rather than a risk limit. Widening it to
    # the calibrated level lifted avg/trade from -0.05% to +0.27% AND improved the
    # worst case (-2.00% -> -1.78%), because fewer recoverable dips were converted
    # into realised losses. See references/exit-timing-and-payoff.md.
    # atr_pct is a PERCENT (e.g. 0.4 = 0.4%); convert to fraction for stop math.
    raw_stop = (atr_pct / 100.0) * 1.2 if atr_pct > 0 else stop_floor
    stop_pct = min(stop_ceil, max(stop_floor, raw_stop))
    # TP keeps its ORIGINAL scale and is deliberately NOT derived from the widened
    # stop. The measured fix was to widen the STOP while leaving the reward target
    # where it was: deriving TP from a 2.5% stop pushes it to +4.5–6.25%, but our
    # average peak gain is only +1.08%, so that target can never be reached. The
    # verified configuration is TP ~+2% (unchanged), stop 2.5%, hold 2h.
    tp_stop = min(0.025, max(0.010, raw_stop))

    # 2) REWARD:RISK: momentum/trend adaptive — ride strong moves harder
    if mom > 0.6 and trend > 0:
        rr = 2.5          # strong momentum + uptrend → let it run
    elif mom > 0.15:
        rr = 2.2
    else:
        rr = 1.8          # weak/flat → take profit sooner, don't give back
    tp_pct = tp_stop * rr

    # 3) SIZE: bigger when calm (tight stop = small $ risk) + momentum confirms.
    # Risk ~$0.13/trade hard ceiling at $7; scale DOWN in wild volatility
    # instead of up (wild = unpredictable). Base $7.
    vol_factor = max(0.6, min(1.2, 1.4 - atr_pct / 3.0))
    conv_factor = 1.15 if (mom > 0.3 and trend > 0 and sig.get("rsi", 50) < 65) else 1.0
    # SHARP-CONFIDENCE BOOST (2026-09-09): RSI<=35 deep-dip on 15m backtested
    # at 87-93% WR (ETH/XRP/SOL). Owner mandate: 'if signal is guaranteed to
    # win, take more risk'. MAX_SIZE_USDT is the hard ceiling — a sharp signal
    # sizes UP to it, a loose one stays at base. Never exceeds the cap.
    if sig.get("sharp"):
        conv_factor *= 1.35  # up to +35% size on the near-guaranteed setups
    size = round(base_size * vol_factor * conv_factor, 1)
    size = min(size, MAX_SIZE_USDT)
    # never drop below Binance's $5 min-notional comfort (we need ~$6+ to round)
    if size < 6.0:
        size = 6.0 if base_size >= 6.0 else base_size

    return {"size": size, "stop_pct": round(stop_pct, 4), "tp_pct": round(tp_pct, 4),
            "tp": round(entry * (1 + tp_pct), 4),
            "sl": round(entry * (1 - stop_pct), 4)}


def open_long(symbol: str, size_usdt: float | None = None, atr: float | None = None, sig: dict | None = None) -> dict:
    """Real market BUY with computed TP/SL. Returns trade record or error dict.

    ATOMICITY: wraps the check-buy-save sequence in an flock so two concurrent
    processes (cron scan + manager restart, or overlapping scans) can NEVER
    both pass the max-open check before either saves state. This was the root
    cause of the 2026-09-05 double-open bug (2x XRP 4s apart, same entry).
    """
    if not live_enabled():
        return {"ok": False, "reason": "live disabled (no config/LIVE_ENABLED flag)"}
    lock_path = ROOT / "data" / ".live_open.lock"
    try:
        import fcntl
        lfh = open(lock_path, "w")
        fcntl.flock(lfh.fileno(), fcntl.LOCK_EX)  # blocks until lock held
    except Exception:
        lfh = None  # no fcntl (windows?) — proceed without lock
    try:
        return _open_long_locked(symbol, size_usdt, atr, sig)
    finally:
        try:
            if lfh is not None:
                fcntl.flock(lfh.fileno(), fcntl.LOCK_UN)
                lfh.close()
        except Exception:
            pass


def _open_long_locked(symbol: str, size_usdt: float | None, atr: float | None, sig: dict | None = None) -> dict:
    s = _load()
    # GUARD 0: never hold two concurrent positions on the SAME symbol (diversify
    # the $16 across 2 different coins — double-XRP = double risk on one coin).
    if any(p["symbol"] == symbol for p in s["open"]):
        return {"ok": False, "reason": f"{symbol} already has an open position"}
    # GUARD 1: never exceed max open positions
    if len(s["open"]) >= LIVE_MAX_OPEN:
        return {"ok": False, "reason": f"max open ({LIVE_MAX_OPEN}) reached"}
    # GUARD 2: daily loss cap — halt trading if today's realized loss >= cap
    todays_loss = sum(
        t.get("pnl", 0) for t in s["closed"]
        if t.get("close_ts_utc", "").startswith(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        and t.get("pnl", 0) < 0
    )
    if abs(todays_loss) >= LIVE_MAX_DAILY_LOSS:
        return {"ok": False, "reason": f"daily loss cap ${LIVE_MAX_DAILY_LOSS} hit (lost ${todays_loss:.2f}) — halted"}
    size = size_usdt or DEFAULT_SIZE_USDT
    size = min(size, MAX_SIZE_USDT)
    # Only pairs where a ~$6 order survives Binance lot-rounding to clear the
    # $5 min notional. VERIFIED LIVE 2026-09-04 at $6 size (notional after rounding):
    #   XRP $5.98  SOL $5.96  DOGE $6.00  ADA $5.99  LINK $5.94
    #   AVAX $5.94 BNB $5.76  SUI $5.92  TON $6.00  TRX $5.97
    if symbol not in ALLOWED_REAL:
        return {"ok": False, "reason": f"{symbol} not tradeable at ${size} min (lot rounding < $5 notional)"}
    try:
        bal = live_balances()
        usdt = float(bal.get("USDT", 0.0)) + float(bal.get("USD", 0.0)) + float(bal.get("USDC", 0.0))
        if usdt - size < LIVE_MIN_FREE:
            return {"ok": False, "reason": f"would drop free below ${LIVE_MIN_FREE} buffer (free={usdt:.2f})"}
        buy = place_market_buy(symbol, size)
        if buy.get("status") not in ("FILLED", "filled", None):
            pass
        entry = buy.get("price", 0.0)
        if not entry:
            return {"ok": False, "reason": "no fill price", "raw": buy}
        # ADAPTIVE TP/SL/size from volatility + momentum + trend (2026-09-06).
        # Explicit size_usdt (manual call) still wins; otherwise scale by context.
        plan = _adaptive_plan(sig, entry)
        if size_usdt is None:
            size = plan["size"]
        tp = plan["tp"]
        sl = plan["sl"]
        rec = {
            "symbol": symbol,
            "side": "long",
            "entry": round(entry, 4),
            "size_usdt": round(size, 2),
            "qty": buy.get("amount"),
            "tp": tp,
            "sl": sl,
            "open_ts_utc": datetime.now(timezone.utc).isoformat(),
            "order_id": buy.get("id"),
        }
        s["open"].append(rec)
        _save(s)
        return {"ok": True, "trade": rec}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"open_long error: {str(e)[:120]}"}


def daily_realized_loss() -> float:
    """Sum of negative PnL closed today (helper for reporting)."""
    s = _load()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(
        t.get("pnl", 0) for t in s["closed"]
        if t.get("close_ts_utc", "").startswith(today) and t.get("pnl", 0) < 0
    )


def _notify_close(pos: dict, pnl: float, reason: str) -> None:
    """Instant Telegram alert on every REAL close (owner: 'make real profit and
    report me'). Best-effort, never blocks manage() on failure.

    Renders the HUMAN view: what happened, what it earned in dollars, and why —
    not a field dump. This is the most important message the system sends, so it
    should read like a sentence, not a log line.
    """
    icon = "🟢" if pnl >= 0 else "🔴"
    emoji = {"TP": "🎯", "SL": "🛑", "TIME": "⏱", "SOLD-EXCHANGE": "🔄"}.get(reason, "📤")
    try:
        from trading.tg.bot_ui import send_card
        sym = pos.get("symbol", "?")
        entry = pos.get("entry", 0) or 0
        exit_px = pos.get("exit", 0) or 0
        ret = (exit_px / entry - 1) if entry else None
        held = None
        try:
            o = pos.get("open_ts_utc") or pos.get("ts_utc")
            if o:
                od = datetime.fromisoformat(str(o).replace("Z", "+00:00"))
                if od.tzinfo is None:
                    od = od.replace(tzinfo=timezone.utc)
                held = (datetime.now(timezone.utc) - od).total_seconds() / 3600
        except Exception:  # noqa: BLE001
            held = None

        try:
            from trading.tg import human as H
            title = f"{emoji} Sold {sym}"
            body = H.exit_card(sym, ret, pnl, H.why_plain(reason), held_hours=held,
                               entry=entry, exit_px=exit_px)
            body += (f"\n\n<i>💰 Real money · "
                     f"{len(_load()['closed'])} trades closed so far</i>")
        except Exception:  # noqa: BLE001 — never fail the alert on formatting
            title = f"{icon} REAL CLOSE {pnl:+.2f}"
            body = (f"{sym} {reason} | {pnl:+.2f} USDT\n"
                    f"{entry} → {exit_px}")
        send_card("alerts", title, body)
    except Exception:
        pass  # never block the trade loop on a notification failure


def manage() -> dict:
    """Check all open live positions; place real SELL on TP/SL/time-stop.

    SELF-HEAL: before TP/SL checks, reconcile state vs ACTUAL exchange
    holdings. If an open position's base asset is no longer held (free balance
    ~0), the position was already sold — typically a previous manage() run
    placed the SELL but got OOM-killed before _save(). Without this, the
    ghost position would stay 'open' forever and block new trades. We archive
    it at the real exit price pulled from recent trade history.
    """
    if not live_enabled():
        return {"ok": False, "reason": "live disabled"}
    s = _load()
    events = []
    try:
        ex = make_live_exchange()
        bal = ex.fetch_balance()
        keep = []
        for pos in s["open"]:
            sym = pos["symbol"]
            base = sym.split("/")[0]
            try:
                px = float(ex.fetch_ticker(sym)["last"])
            except Exception:
                keep.append(pos)
                continue
            # --- SELF-HEAL reconcile: do we still hold the base asset? ---
            try:
                held_free = float(bal.get(base, {}).get("free", 0))
            except Exception:
                held_free = -1.0  # unknown -> trust state
            expected_qty = float(pos.get("qty", 0) or 0)
            if held_free >= 0 and expected_qty > 0 and held_free < expected_qty * 0.1:
                # Position's coins are gone from the exchange but state says open.
                # Find the real sell in recent trade history to record true PnL.
                real_px, real_qty = None, 0.0
                try:
                    my = ex.fetch_my_trades(sym, limit=20)
                    opened = pos.get("open_ts_utc", "")
                    for t in my:
                        if t.get("side") == "sell" and t.get("datetime", "") >= opened[:19]:
                            real_px = float(t.get("price"))
                            real_qty = float(t.get("amount"))
                            break
                except Exception:
                    pass
                exit_px = real_px if real_px else px
                closed_qty = real_qty if real_qty else expected_qty
                pnl = (exit_px - pos["entry"]) * closed_qty
                pos["exit"] = round(exit_px, 6)
                pos["pnl"] = round(pnl, 4)
                pos["reason"] = "SOLD-EXCHANGE"
                pos["close_ts_utc"] = datetime.now(timezone.utc).isoformat()
                s["closed"].append(pos)
                events.append(f"HEAL {sym}: coins gone (held {held_free:.6f} < 10% of {expected_qty}) — archived at {exit_px} pnl={pnl:+.2f}")
                _notify_close(pos, pnl, "SOLD-EXCHANGE")
                continue
            # --- BREAKEVEN/TRAIL PROTECTION (2026-09-07): a position that
            # moved in our favor must NEVER give it all back. Data: 3 real
            # trades went green then exited RED (BNB -0.09, XRP -0.02, BNB
            # -0.04) — pure give-back, zero protection. Rules:
            #   >= +0.5%  → SL raised to entry (worst case = breakeven)
            #   >= +1.5%  → SL trails at +0.8% locked (bank partial profit)
            # Applied BEFORE TP/SL checks so a raised SL can trigger same pass.
            pnl_pct = (px - pos["entry"]) / pos["entry"] * 100
            if pnl_pct >= 1.5 and pos.get("sl", 0) < pos["entry"] * 1.008:
                pos["sl"] = round(pos["entry"] * 1.008, 4)  # lock +0.8%
            elif pnl_pct >= 0.5 and pos.get("sl", 0) < pos["entry"]:
                pos["sl"] = round(pos["entry"], 4)  # lock breakeven
            # --- MOMENTUM-STALL EXIT (2026-09-09): a green position that stops
            # making progress holds the slot hostage while better setups queue.
            # Track best price since open; if green (+0.3%) but no NEW HIGH in
            # ~90 min, momentum died → bank the profit, free the slot for the
            # next elite signal. If still climbing, keep riding to TP.
            # Owner: 'exit before TP to maximize time and switch to better spot'.
            best = float(pos.get("best_px") or pos["entry"])
            if px > best:
                pos["best_px"] = round(px, 6)
                pos["best_ts_utc"] = datetime.now(timezone.utc).isoformat()
                best = px
            stall_reason = None
            if pnl_pct >= 0.3 and best > pos["entry"] * 1.003:
                bts = pos.get("best_ts_utc")
                if bts:
                    try:
                        stall_min = (datetime.now(timezone.utc) -
                                     datetime.fromisoformat(bts)).total_seconds() / 60.0
                        if stall_min >= 90:
                            stall_reason = "STALL"
                    except Exception:
                        pass
                else:
                    # no high recorded yet but green — record now, re-check next pass
                    pos["best_px"] = round(px, 6)
                    pos["best_ts_utc"] = datetime.now(timezone.utc).isoformat()
            # --- normal TP/SL/time management ---
            reason = None
            if px >= pos["tp"]:
                reason = "TP"
            elif px <= pos["sl"]:
                reason = "SL"
            elif stall_reason:
                reason = "STALL"  # bank green profit, recycle the slot
            elif _hold_expired(pos):
                # WINNER-PROTECTION: a green position (up >= +0.5%) keeps
                # running toward TP up to 12h instead of time-exiting at ~+0.3%.
                # The old 6h TIME exit dumped 72 winners at +0.31% avg before
                # they reached TP +2.58% — the #1 profit leak (wins +0.63% vs
                # losses -0.84% = negative EV even at 55% WR). Fixed 2026-09-06.
                pnl_pct = (px - pos["entry"]) / pos["entry"] * 100
                if pnl_pct >= 0.5 and not _hold_expired(pos, hours=12.0):
                    reason = None  # let the winner run
                else:
                    reason = "TIME"
            if reason:
                # Sell the ACTUAL free balance of the base asset (most reliable
                # — avoids lot/notional/balance mismatches from stored/computed qty).
                try:
                    free = float(bal.get(base, {}).get("free", 0))
                except Exception:
                    free = 0.0
                if free <= 0:
                    free = pos["size_usdt"] / pos["entry"]
                try:
                    step = float(ex.market(sym)["precision"]["amount"])
                    free = (int(free / step) * step) if step > 0 else round(free, 8)
                except Exception:
                    pass
                qty = round(free, 8)
                if qty <= 0:
                    keep.append(pos)
                    continue
                sell = place_market_sell(sym, float(qty))
                pnl = (px - pos["entry"]) * float(qty)
                pos["exit"] = round(px, 4)
                pos["pnl"] = round(pnl, 4)
                pos["reason"] = reason
                pos["close_ts_utc"] = datetime.now(timezone.utc).isoformat()
                s["closed"].append(pos)
                events.append(f"SELL {sym} ({reason}) pnl={pnl:+.2f} USDT")
                _notify_close(pos, pnl, reason)
            else:
                keep.append(pos)
        s["open"] = keep
        _save(s)
        return {"ok": True, "events": events, "open": len(s["open"]),
                "closed": len(s["closed"])}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"manage error: {str(e)[:120]}"}


def _hold_expired(pos: dict, hours: float | None = None) -> bool:
    from datetime import datetime as dt
    try:
        opened = dt.fromisoformat(pos["open_ts_utc"])
        age_h = (dt.now(timezone.utc) - opened).total_seconds() / 3600
        if hours is None:
            # self-calibrated hold (scripts/exit_calibrate.py). `hours` is passed
            # explicitly for the 12h winner-protection path, which must NOT move.
            hours = _exit_params()["hold_hours"]
        return age_h >= hours
    except Exception:  # noqa: BLE001
        return False


def summary() -> dict:
    s = _load()
    wins = sum(1 for t in s["closed"] if t.get("pnl", 0) > 0)
    losses = sum(1 for t in s["closed"] if t.get("pnl", 0) <= 0)
    total_pnl = sum(t.get("pnl", 0) for t in s["closed"])
    return {
        "live_enabled": live_enabled(),
        "open_positions": len(s["open"]),
        "closed_trades": len(s["closed"]),
        "wins": wins,
        "losses": losses,
        "total_pnl": round(total_pnl, 4),
        "open": s["open"],
    }


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if mode == "summary":
        print(json.dumps(summary(), indent=2))
    elif mode == "manage":
        print(json.dumps(manage(), indent=2))
    else:
        print(json.dumps({"usage": "live_executor.py [summary|manage]"}, indent=2))
