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

# F1 (docs/loop/designs/T0.3.md): never buy the same coin twice after a crash.
LOCK = BASE / "data" / "trend-follower.lock"
DUST_USD = 1.0            # a wallet coin worth less than this does not count as held (owner, 2026-09-27)
INTENT_TRUST_H = 24       # "order not found" is trusted only for intents younger than this


class StateCorrupt(Exception):
    """trend-state.json exists but can't be read. Trading must stop: an empty book would re-buy."""


def sma(v, n):
    return sum(v[-n:]) / n


def coin_trend(coin_closes):
    """The coin's OWN trend condition, without the market regime filter.

    Split out of `signal()` to fix a measured defect. `signal()` ANDs two
    conditions — the coin above SMA50 and BTC above SMA200 — and the exit path
    used `not want`, so it fired when EITHER broke. That meant the market-regime
    filter closed positions that had not individually broken.

    Measured cost on the real record (9 closes, 2026-09-13 to 09-20):

        8 of 9 exits were `trend-broke`, averaging -3.42%
        only 1 was a genuine HARD-STOP (-15.6%)
        SUI closed 3x, ATOM 2x, SAND 2x -> exiting and immediately re-entering
        all exits landed on the 00:10 / 08:10 / 16:10 run, i.e. a 4h cadence

    So the losses were dominated by whipsaw around the BTC SMA200 line, not by
    individual coins losing their trends. A regime filter is a reasonable ENTRY
    gate — do not open into a hostile market — but as an EXIT it converts a
    market-wide flicker into a position closure and a fresh round trip.

    Exits now depend on the coin's own trend (plus the -15% disaster stop), which
    is the condition that was actually being bet on.
    """
    if len(coin_closes) < 60:
        return False
    return coin_closes[-1] > sma(coin_closes, SMA_N)


def signal(coin_closes, btc_closes):
    """Long if coin above SMA50 AND BTC above SMA200 (regime filter)."""
    if len(coin_closes) < 60 or len(btc_closes) < 205:
        return False
    return (coin_trend(coin_closes)
            and btc_closes[-1] > sma(btc_closes, 200))


def load():
    """The saved state, or a fresh one on the very first run.

    A file that exists but can't be parsed raises StateCorrupt. It used to come back as an empty
    book, and the next run then bought up to MAX_POS coins on top of the ones already held.
    """
    if not STATE.exists():
        return {"positions": {}, "log": [], "real": {"open": [], "pending": []}}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise StateCorrupt(f"{STATE}: {type(e).__name__}: {e}") from e


def save(s):
    """Atomic: the file is always the old version or the new one, never half-written."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(s, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE)


def _acquire_lock(path):
    """Exclusive non-blocking lock; None if another run holds it. The OS drops it if the process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:                       # Windows (dev and tests)
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return None
    return fh


def _release_lock(fh):
    if fh is None:
        return
    try:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except ImportError:
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    fh.close()


def _new_cid(symbol):
    import uuid
    return f"tf-{symbol.split('/')[0]}-{datetime.now(timezone.utc):%Y%m%d%H%M}-{uuid.uuid4().hex[:6]}"


def _open_record(symbol, od, requested_qty, cid):
    """A real position built from the exchange's fill. Missing values are UNKNOWN, never invented."""
    filled, avg = od.get("filled"), od.get("average")
    rec = {"symbol": symbol, "cid": cid, "oid": str(od.get("id", "")),
           "ts": datetime.now(timezone.utc).isoformat()}
    if filled:
        rec.update(qty=float(filled), qty_status="FILL")
    else:
        rec.update(qty=float(requested_qty), qty_status="REQUESTED")   # what was sent; the fill is unknown
    if avg:
        rec.update(entry=float(avg), entry_status="FILL")
    else:
        rec.update(entry=None, entry_status="UNKNOWN")
    return rec


def _resolve_pending(ex, real, events):
    """Settle every buy intent left by an earlier run. Returns True if new buys must be blocked this run."""
    try:
        from ccxt import OrderNotFound
    except ImportError:                           # pragma: no cover - ccxt is a hard dependency in production
        OrderNotFound = type("OrderNotFound", (Exception,), {})
    blocked = False
    keep = []
    for it in real.get("pending", []):
        try:
            od = ex.fetch_order(None, it["symbol"], params={"clientOrderId": it["cid"]})
        except OrderNotFound:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(it["ts"])).total_seconds() / 3600
            if age_h < INTENT_TRUST_H:
                events.append(("ERR", it["symbol"], None, f"intent {it['cid']} never reached Binance: dropped"))
                continue
            keep.append(it)
            blocked = True
            events.append(("ERR", it["symbol"], None,
                           f"intent {it['cid']} is {age_h:.0f}h old and not found: kept, buys blocked"))
            continue
        except Exception as e:  # noqa: BLE001 - unknown state: keep it and buy nothing
            keep.append(it)
            blocked = True
            events.append(("ERR", it["symbol"], None, f"can't check {it['cid']} ({type(e).__name__}): buys blocked"))
            continue
        status = str(od.get("status", "")).lower()
        if status == "closed":
            real["open"].append(_open_record(it["symbol"], od, it["qty"], it["cid"]))
            events.append(("ERR", it["symbol"], od.get("average"), f"recovered buy {it['cid']} from Binance"))
        elif status in ("canceled", "cancelled", "rejected", "expired") and not (od.get("filled") or 0):
            events.append(("ERR", it["symbol"], None, f"intent {it['cid']} {status} with no fill: dropped"))
        else:                                      # open / partially filled / odd: wait, add no risk meanwhile
            keep.append(it)
            blocked = True
            events.append(("ERR", it["symbol"], None, f"order {it['cid']} is {status or 'UNKNOWN'}: buys blocked"))
    real["pending"] = keep
    return blocked


def _wallet_held(bal, prices):
    """REAL_SYMS coins the wallet actually holds (value >= DUST_USD). A coin with no known price counts as held."""
    n = 0
    for s in REAL_SYMS:
        base = s.split("/")[0]
        total = float((bal.get(base) or {}).get("total") or 0)
        if total <= 0:
            continue
        px = prices.get(s)
        if px is None or total * px >= DUST_USD:
            n += 1
    return n


def _ccxt_error(name):
    try:
        import ccxt
        return getattr(ccxt, name)
    except (ImportError, AttributeError):          # tests stub ccxt; a class nothing raises is harmless
        return type(name, (Exception,), {})


def _real_exit_reason(rp, closes, pos):
    """(why, stop_basis) if this REAL position must be sold now, else (None, basis). Uses the real book."""
    if not closes:
        return None, None
    px = closes[-1]
    basis, ent = "real", rp.get("entry")
    if not ent:                                    # entry UNKNOWN (F1): measure the stop from paper, and say so
        basis, ent = "paper", (pos.get(rp["symbol"]) or {}).get("entry") or None
    if ent and px / ent - 1 <= -HARD_STOP:
        return "HARD-STOP", basis
    if not coin_trend(closes):
        return "trend-broke", basis
    return None, basis


def _fees(items):
    """(fee_usd, status, raw) from ccxt fee dicts. Only a USDT fee has a USD value we didn't invent."""
    raw = [{"cost": f.get("cost"), "currency": f.get("currency")} for f in items
           if f and f.get("cost") is not None]
    if raw and all(f["currency"] == "USDT" for f in raw):
        return round(sum(float(f["cost"]) for f in raw), 8), "FILL", raw
    return None, "UNKNOWN", raw                    # e.g. paid in BNB: converting it would need a price we don't have


def _close_real(real, rp, rec, events):
    """Finish a real.closed record (PnL only from known values), move it out of real.open, emit the event."""
    entry, ex_px, qty = rp.get("entry"), rec.get("exit"), rec.get("qty")
    rec.update({"book": "real", "symbol": rp["symbol"], "entry": entry,
                "entry_status": rp.get("entry_status", "BOOKED"),   # pre-F1 entries were the candle px, not a fill
                "open_ts": rp.get("ts"), "close_ts": datetime.now(timezone.utc).isoformat()})
    gross = round((ex_px - entry) * qty, 8) if entry and ex_px and qty else None
    rec["pnl_gross_usd"] = gross
    rec["pnl_usd"] = round(gross - rec["fee_usd"], 8) if gross is not None and rec.get("fee_usd") is not None else None
    real.setdefault("closed", []).append(rec)
    real["open"].remove(rp)
    ret = (ex_px / entry - 1) if ex_px and entry else None
    events.append(("EXIT", rp["symbol"], ex_px, {"ret": ret, "why": rec["reason"], "real": True}))


def _book_real_exit(real, rp, od, events):
    """Move a sold position to real.closed from the SELL fill. Unknown values stay null + UNKNOWN."""
    ep = rp["exit_pending"]
    filled, avg = od.get("filled"), od.get("average")
    fee_usd, fee_status, fee_raw = _fees(od.get("fees") or ([od["fee"]] if od.get("fee") else []))
    rec = {"cid": ep["cid"], "order_id": str(od.get("id", "")),
           "qty": float(filled) if filled else None, "qty_status": "FILL" if filled else "UNKNOWN",
           "exit": float(avg) if avg else None, "exit_status": "FILL" if avg else "UNKNOWN",
           "fee_usd": fee_usd, "fee_status": fee_status, "fee_raw": fee_raw,
           "reason": ep["why"], "stop_basis": ep.get("stop_basis"), "attempts": ep["attempts"]}
    _close_real(real, rp, rec, events)


def _close_without_sell(ex, real, rp, free, events):
    """The coins are already gone (sold outside this record). Close it from OUR trade history, or as UNKNOWN.

    Never priced from the current candle: that fallback is the F4 defect in live_executor.
    """
    s, booked = rp["symbol"], float(rp["qty"])
    ep = rp.get("exit_pending") or {}
    rec = {"reason": "NO_SELLABLE_BALANCE", "trigger": ep.get("why"), "free": free, "booked": booked,
           "qty": None, "qty_status": "UNKNOWN", "exit": None, "exit_status": "UNKNOWN",
           "fee_usd": None, "fee_status": "UNKNOWN", "fee_raw": [], "trade_ids": []}
    try:
        since = int(datetime.fromisoformat(rp["ts"]).timestamp() * 1000)
        trades = [t for t in ex.fetch_my_trades(s, since=since)
                  if t.get("side") == "sell" and (t.get("timestamp") or 0) >= since]
        step = float((ex.market(s).get("precision") or {}).get("amount") or 0)
        missing = booked - free
        sold = sum(float(t.get("amount") or 0) for t in trades)
        if trades and abs(sold - missing) <= step + 1e-12:
            vwap = sum(float(t["amount"]) * float(t["price"]) for t in trades) / sold
            fee_usd, fee_status, fee_raw = _fees([t.get("fee") for t in trades])
            rec.update(qty=sold, qty_status="TRADE_HISTORY", exit=round(vwap, 10), exit_status="TRADE_HISTORY",
                       fee_usd=fee_usd, fee_status=fee_status, fee_raw=fee_raw,
                       trade_ids=[str(t.get("id")) for t in trades])
        else:
            rec["exit_resolve_error"] = (f"sells since open total {sold:g}, missing {missing:g} "
                                         f"(step {step:g}): not reconciled")
    except Exception as e:  # noqa: BLE001 - recorded as UNKNOWN; a human decides whether to dig further
        rec["exit_resolve_error"] = f"{type(e).__name__}: {str(e)[:80]}"
    _close_real(real, rp, rec, events)
    if rec["exit_status"] == "UNKNOWN":
        notify(f"⚠️ Real exit UNKNOWN: {s}",
               f"The wallet no longer holds it (free={free:g}, booked={booked:g}) and the exit could not be "
               f"reconstructed from trade history: {rec['exit_resolve_error']}\nRecorded as UNKNOWN, not guessed.")


def _block_exit(rp, reason, events):
    """Stop retrying a sell that can't succeed. The position stays in real.open and a human decides."""
    rp["exit_blocked"] = {"since": datetime.now(timezone.utc).isoformat(), "reason": reason}
    events.append(("ERR", rp["symbol"], None, f"real exit blocked: {reason}"))
    notify(f"⛔ Real exit blocked: {rp['symbol']}",
           f"{reason}\nThe position is still tracked; it won't be retried until a human clears exit_blocked.")


def _real_exits(ex, st, closes_by, events):
    """Sell real positions whose trend broke or whose hard stop hit. Returns how many were sold.

    Decided from real.open, not the paper flag. A failed sell keeps exit_pending and is retried
    next run; a retry first looks up the previous sell's clientOrderId, so it never sells twice.
    """
    real, pos = st["real"], st["positions"]
    OrderNotFound = _ccxt_error("OrderNotFound")
    InvalidOrder = _ccxt_error("InvalidOrder")
    NetworkError = _ccxt_error("NetworkError")    # includes RateLimitExceeded and RequestTimeout
    sold = 0
    for rp in list(real["open"]):
        s = rp["symbol"]
        if rp.get("exit_blocked"):
            continue
        ep = rp.get("exit_pending")
        if ep and ep.get("cid"):
            try:
                od = ex.fetch_order(None, s, params={"clientOrderId": ep["cid"]})
            except OrderNotFound:                  # must precede InvalidOrder: it is a subclass of it
                od = None                          # the last sell never reached Binance: send again
            except Exception as e:  # noqa: BLE001 - can't prove the last sell failed, so don't send another
                ep["last_error"] = f"{type(e).__name__}: {str(e)[:80]}"
                events.append(("ERR", s, None, f"can't check sell {ep['cid']}: not re-sent this run"))
                continue
            if od is not None:
                status = str(od.get("status", "")).lower()
                if status == "closed":
                    _book_real_exit(real, rp, od, events)
                    sold += 1
                    save(st)
                    continue
                if not (status in ("canceled", "cancelled", "rejected", "expired") and not (od.get("filled") or 0)):
                    events.append(("ERR", s, None, f"sell {ep['cid']} is {status or 'UNKNOWN'}: waiting"))
                    continue
        if not ep:
            why, basis = _real_exit_reason(rp, closes_by.get(s), pos)
            if not why:
                continue
            ep = rp["exit_pending"] = {"since": datetime.now(timezone.utc).isoformat(), "attempts": 0,
                                       "why": why, "stop_basis": basis}
        try:
            # Sell only what is actually there: DOGE was booked at 58 while the wallet held 23.
            free = float((ex.fetch_balance().get(s.split("/")[0]) or {}).get("free") or 0)
            qty = min(float(rp["qty"]), free)
            mmin = float(ex.market(s).get("limits", {}).get("amount", {}).get("min") or 0)
        except Exception as e:  # noqa: BLE001
            ep["last_error"] = f"{type(e).__name__}: {str(e)[:80]}"
            save(st)
            events.append(("ERR", s, None, f"can't size the sell ({type(e).__name__}): retry next run"))
            continue
        if qty <= 0 or qty < mmin:
            _close_without_sell(ex, real, rp, free, events)   # F3 (T0.5): recorded, never silently deleted
            save(st)
            continue
        ep["cid"] = _new_cid(s)
        ep["attempts"] += 1
        ep["qty"] = qty
        save(st)                                   # write-ahead, as for buys
        try:
            od = ex.create_order(s, "market", "sell", qty, params={"clientOrderId": ep["cid"]})
        except InvalidOrder as e:
            _block_exit(rp, f"{type(e).__name__}: {str(e)[:80]}", events)
            save(st)
            continue
        except Exception as e:  # noqa: BLE001 - it may have executed; the cid lookup next run decides
            ep["last_error"] = f"{type(e).__name__}: {str(e)[:80]}"
            save(st)
            events.append(("ERR", s, None, f"sell failed (attempt {ep['attempts']}): {ep['last_error'][:60]}"))
            if ep["attempts"] == 3 or (ep["attempts"] == 1 and not isinstance(e, NetworkError)):
                notify(f"⚠️ Real sell failing: {s}",
                       f"Attempt {ep['attempts']}: {ep['last_error']}\nStill tracked; retried every run.")
            continue
        _book_real_exit(real, rp, od, events)
        sold += 1
        save(st)
    return sold


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
                        f" at {H.money(px) if px is not None else 'UNKNOWN price'}"
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
    lock = _acquire_lock(LOCK)
    if lock is None:
        print("previous trend_follower run still active: exiting without trading")
        notify("⚠️ Trend follower skipped a run", "The previous run is still active, so this one did nothing.")
        sys.exit(3)
    try:
        _run()
    finally:
        _release_lock(lock)


def _run():
    from trading.exchange.live_client import make_live_exchange
    try:
        st = load()
    except StateCorrupt as e:
        print(f"trend-state.json is unreadable, NOT trading: {e}")
        notify("⛔ Trend follower stopped",
               "trend-state.json is unreadable, so no order was placed. Restore it from the nightly backup. "
               "Do NOT delete it: an empty state would re-buy coins that are already held.")
        sys.exit(2)
    ex = make_live_exchange()
    pos = st.setdefault("positions", {})
    real = st.setdefault("real", {"open": []})
    real.setdefault("pending", [])
    events = []
    buys_blocked = False
    if REAL:
        buys_blocked = _resolve_pending(ex, real, events)
        save(st)                                   # persist what was resolved before doing anything else

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
    bal = {}
    if REAL:
        try:
            bal = ex.fetch_balance()
            free_usdt = float(bal.get("USDT", {}).get("free", 0))
        except Exception:
            buys_blocked = True                   # can't see the wallet, so can't prove there's room

    candidates = []   # (momentum, symbol, px) for real ranking
    last_px = {}
    closes_by = {}    # symbol -> daily closes, reused by the real exit pass
    for s in SYMS:
        try:
            o = ex.fetch_ohlcv(s, "1d", limit=300)
            if not o or len(o) < 60:
                continue
            closes = [c[4] for c in o]
            px = closes[-1]
            last_px[s] = px
            want = signal(closes, btc)
            # Enter requires BOTH the coin's own trend and the market regime.
            #
            # EXIT requires only the coin's own trend. See the note below — the
            # asymmetry is deliberate and is the fix for a measured defect.
            coin_ok = coin_trend(closes)
            held = pos.get(s, {}).get("long", False)

            ent_held = pos.get(s, {}).get("entry", px) or px
            hard_hit = held and (px / ent_held - 1) <= -HARD_STOP

            if want and not held:
                pos[s] = {"long": True, "entry": px,
                          "ts": datetime.now(timezone.utc).isoformat()}
                events.append(("OPEN", s, px, None))
                mom = closes[-1] / closes[-31] - 1 if len(closes) > 31 else 0.0
                candidates.append((mom, s, px))
            elif (not coin_ok and held) or hard_hit:
                ent = ent_held
                ret = px / ent - 1
                why = "HARD-STOP" if hard_hit else "trend-broke"
                st["log"].append({"symbol": s, "entry": ent, "exit": px,
                                  "ret": round(ret, 5), "why": why,
                                  "ts": datetime.now(timezone.utc).isoformat()})
                pos[s] = {"long": False, "entry": 0,
                          "ts": datetime.now(timezone.utc).isoformat()}
                # Tag whether this exit was REAL money, so the notification can
                # separate real actions from paper ones. Without it the owner sees
                # a real sell and a paper sell in the same undifferentiated list.
                # Paper only. Real money is sold by _real_exits() from real.open (F2, T0.4):
                # selling here, behind the paper flag, stranded a real position with no
                # stop whenever the sell failed after the flag had already flipped.
                events.append(("EXIT", s, px, {"ret": ret, "why": why, "real": False}))
            closes_by[s] = closes
        except Exception:
            continue

    if REAL and _real_exits(ex, st, closes_by, events):
        try:
            bal = ex.fetch_balance()               # coins just sold no longer count toward MAX_POS
        except Exception:
            pass                                   # keep the older, more conservative balance

    # REAL EXECUTION — open the top MAX_POS candidates ranked by 30d momentum,
    # but ONLY from REAL_SYMS. `candidates` is drawn from the full paper universe,
    # so without this gate widening SYMS would quietly widen real-money exposure —
    # which is the exact opposite of the intent.
    if REAL and candidates and buys_blocked:
        events.append(("ERR", "trend", None, "new buys blocked this run until earlier orders are resolved"))
    if REAL and candidates and not buys_blocked:
        candidates.sort(reverse=True)
        held_now = max(len(real["open"]) + len(real["pending"]), _wallet_held(bal, last_px))
        for mom, s, px in candidates:
            if s not in REAL_SYMS:
                continue                      # paper-only until promoted
            if any(r["symbol"] == s for r in real["open"] + real["pending"]):
                continue                      # never hold the same coin twice
            if held_now >= MAX_POS:           # counts what the WALLET holds, not only what the state file says
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
                # write-ahead: the intent is on disk BEFORE the order, so a crash can't forget it
                cid = _new_cid(s)
                intent = {"cid": cid, "symbol": s, "qty": qty,
                          "ts": datetime.now(timezone.utc).isoformat(), "state": "sent"}
                real["pending"].append(intent)
                save(st)
                od = ex.create_order(s, "market", "buy", qty, params={"clientOrderId": cid})
                real["pending"].remove(intent)
                rec = _open_record(s, od, qty, cid)
                real["open"].append(rec)
                save(st)
                held_now += 1
                free_usdt -= notional
                events.append(("REALBUY", s, rec["entry"] if rec["entry"] is not None else px,
                               {"qty": rec["qty"], "notional": round(notional, 2),
                                "entry_status": rec["entry_status"]}))
            except Exception as e:
                # the order may or may not have executed: its intent stays pending and nothing more is bought
                save(st)
                events.append(("ERR", s, px, f"{str(e)[:60]} (order state unknown: checked next run)"))
                break

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
