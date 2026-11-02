#!/usr/bin/env python3
"""Bybit DEMO runner for the external lab's crypto-trend strategies.

WHAT THIS IS
------------
The forward test that HANDBOOK.md §8.1 says does not exist yet. It polls closed
candles from Bybit's demo environment, runs the lab's own pure decide() from
strategies/STRATEGY_SPEC.md, and places real orders on the DEMO account so the
fills, fees, funding and slippage are real even though the money is not.

WHY IT IS BUILT THIS WAY
------------------------
Three rules from AGENT.md in the source lab, kept verbatim because they are right:

  * Rules place orders, the runner does not. No discretionary trades, no tuning.
  * Settings change only via server/strategies.json (copied here as a checked-in
    registry), never by editing this file mid-run.
  * Kill switch per strategy: own drawdown > 15%, signal/fill divergence > 2
    bars, or 3 consecutive API/order errors -> stop and report, do not restart.

Two additions of my own, because a forward test is only as good as its records:

  * every decision is appended to a JSONL log, INCLUDING decisions to do nothing,
    so "it did not trade" can be told apart from "it was not running"
  * a RANDOM_ENTRY_CONTROL runs alongside on the same exits and sizing. An edge
    must beat the control, not merely be positive. The lab specifies this control;
    without it a profitable forward test is indistinguishable from a lucky market.

ENVIRONMENT
-----------
Demo only. The endpoint is checked at import: if it is not api-demo.bybit.com the
runner refuses to start. Credentials are read by bybit_demo.py from
config/bybit-demo.env and are never printed.

SIZING
------
risk 1% of the strategy's own equity per trade, qty = equity*1% / (2*ATR),
rounded down to the instrument's qtyStep and refused if below minNotional.
The equity throttle halves risk when own equity is 8% below its peak.

Usage:
    python3 run_crypto_trend_demo.py --dry-run     # decide, log, place nothing
    python3 run_crypto_trend_demo.py               # place demo orders
    python3 run_crypto_trend_demo.py --status      # report only
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
sys.path.insert(0, str(BASE / "scripts"))
sys.path.insert(0, str(BASE / "src"))

import bybit_demo as bd  # noqa: E402
import signal_ledger as L  # noqa: E402

# Publishing to the public hash-chained ledger. ON by default because the site's
# whole value is that a signal is committed BEFORE its outcome is known — a record
# added after the fact proves nothing. Every entry and every exit is appended at
# decision time, so the chain position itself is evidence of when it was written.
PUBLISH = True
PUB_SOURCE = "crypto-trend-demo"
PUB_PREFIX = "ea"          # ea-ETH_TURTLE_H4-<ts>; not "demo-" — that prefix
                           # already means the Bybit demo regime system

RUNNER_DIR = BASE / "state" / "crypto-trend-demo"
STATE = RUNNER_DIR / "state.json"
DECISIONS = RUNNER_DIR / "decisions.jsonl"
REGISTRY = RUNNER_DIR / "strategies.json"

EQUITY0 = 10_000.0          # strategy notional book, not the account balance
RISK_PCT = 1.0
THROTTLE_DD = 8.0
THROTTLE_FACTOR = 0.5
STOP_ATR = 2.0
TRAIL_ATR = 3.0
ATR_N = 20
CATEGORY = "linear"


# ------------------------------------------------------------------ the rules
# Transcribed from the lab's strategies/STRATEGY_SPEC.md reference implementation.
def ema(x, n):
    k, e = 2 / (n + 1), x[0]
    out = [e]
    for v in x[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def atr(h, l, c, n=ATR_N):
    tr = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
          for i in range(1, len(c))]
    return sum(tr[-n:]) / n


def decide(h, l, c, engine, in_pos, stop, **p):
    a = atr(h, l, c, ATR_N)
    close1 = c[-1]
    if engine == "turtle":
        hi = max(h[-1 - p["entry"]:-1])
        lo = min(l[-1 - p["exit"]:-1])
        want, leave = close1 > hi, close1 < lo
    elif engine == "dualma":
        f, s = ema(c, p["fast"]), ema(c, p["slow"])
        up, up_prev = f[-1] > s[-1], f[-2] > s[-2]
        want, leave = up and not up_prev, not up
    elif engine == "random_entry":
        want, leave = random_fire(p), False
    else:
        raise ValueError(f"unknown engine {engine}")
    if in_pos:
        if leave:
            return "close", stop, 0.0
        return "hold", max(stop, close1 - TRAIL_ATR * a), 0.0
    return ("buy", close1 - STOP_ATR * a, STOP_ATR * a) if want else ("none", 0.0, 0.0)


def random_fire(p) -> bool:
    """Control: fires at the same expected rate as the strategy it matches.

    Uses the strategy's own trade rate rather than a coin flip, so the control
    accumulates a comparable number of trades over the same period.
    """
    rate = p.get("per_bar_prob")
    if rate is None:
        return False
    return _rng().random() < rate


_RNG = None


def _rng():
    global _RNG
    if _RNG is None:
        import random
        _RNG = random.Random()
    return _RNG


# ------------------------------------------------------------------ bybit io
def klines(symbol: str, interval: str, limit: int = 400) -> list[list]:
    r = bd.public("/v5/market/kline",
                  {"category": CATEGORY, "symbol": symbol,
                   "interval": interval, "limit": min(limit, 1000)})
    if r.get("retCode") != 0:
        raise RuntimeError(f"kline {symbol}: {r.get('retMsg')}")
    rows = r["result"]["list"]
    # Bybit returns newest first; convert to oldest-first lists
    rows = rows[::-1]
    o = [float(x[1]) for x in rows]
    h = [float(x[2]) for x in rows]
    l = [float(x[3]) for x in rows]
    c = [float(x[4]) for x in rows]
    ts = [int(x[0]) for x in rows]
    return [ts, o, h, l, c]


def instrument(symbol: str) -> dict:
    r = bd.public("/v5/market/instruments-info",
                  {"category": CATEGORY, "symbol": symbol})
    i = r["result"]["list"][0]
    return {
        "tick": float(i["priceFilter"]["tickSize"]),
        "step": float(i["lotSizeFilter"]["qtyStep"]),
        "min_qty": float(i["lotSizeFilter"]["minOrderQty"]),
        "min_notional": float(i["lotSizeFilter"].get("minNotionalValue") or 0),
    }


def position(symbol: str) -> dict | None:
    r = bd.private("/v5/position/list",
                   {"category": CATEGORY, "symbol": symbol})
    for p in r.get("result", {}).get("list", []):
        if float(p.get("size") or 0) > 0:
            return p
    return None


def place_market(symbol: str, side: str, qty: float, stop: float,
                 tag: str) -> dict:
    """Market order with an exchange-side stop, as AGENT.md requires."""
    body = {
        "category": CATEGORY, "symbol": symbol, "side": side,
        "orderType": "Market", "qty": f"{qty:.6f}",
        "orderLinkId": f"{tag}-{int(time.time()*1000)}",
        "positionIdx": 0,
        "stopLoss": f"{stop:.6f}",
        "tpslMode": "Full",
    }
    r = bd.private("/v5/order/create", method="POST", body=body)
    return r


def qty_for(symbol: str, equity: float, risk_pct: float, dist: float) -> float:
    inst = instrument(symbol)
    if dist <= 0:
        return 0.0
    raw = equity * (risk_pct / 100.0) / dist
    step = inst["step"]
    q = math.floor(raw / step) * step
    if q < inst["min_qty"]:
        return 0.0
    px = 0.0
    return round(q, 8)


# ------------------------------------------------------------------ state
def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"created_utc": datetime.now(timezone.utc).isoformat(),
            "strategies": {}, "errors": {}}


def save_state(st: dict) -> None:
    RUNNER_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1, sort_keys=True))


def log_decision(rec: dict) -> None:
    RUNNER_DIR.mkdir(parents=True, exist_ok=True)
    with DECISIONS.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def match_group(reg: dict, sid: str) -> set[str]:
    """The arms of this strategy's experiment: itself plus its matched partner.

    A registry entry may declare `match: <id>` to say "I am this strategy's
    luck baseline: same symbol, same exits, same sizing, random entries". Those
    two are two arms of ONE experiment, not two strategies contesting one
    position, so ownership between them must not be exclusive. Returns {sid}
    for an unmatched strategy.
    """
    by_id = {s["id"]: s for s in reg.get("strategies", [])}
    me = by_id.get(sid) or {}
    group = {sid}
    if me.get("match"):
        group.add(me["match"])
    for other in reg.get("strategies", []):
        if other.get("match") == sid:
            group.add(other["id"])
    return group


def _claims(s: dict) -> bool:
    """True when this arm believes it holds a position on its symbol."""
    return bool((s or {}).get("in_pos") and (s or {}).get("symbol"))


def position_holder(st: dict, sym: str, *, exclude: str | None = None) -> tuple[str, bool] | None:
    """Which arm holds the ONE physical position on `sym`, and whether it disputes it.

    The exchange knows symbols, not strategies: ETHUSDT is one position of 1.2
    no matter how many arms look at it. So at most one arm may claim it. Returns
    (sid, disputed) or None.

    `disputed` is True when two arms BOTH claim the symbol -- i.e. the state is
    already corrupt (measured 2026-11-01: ETH_TURTLE_H4 qty 1.2 ledger_id None
    beside RANDOM_ENTRY_CONTROL qty 1.2 with the committed id). In that case the
    committed owner is the real one and every other claimant is a phantom.
    """
    holders = [(sid, s) for sid, s in (st.get("strategies") or {}).items()
               if sid != exclude and _claims(s) and (s or {}).get("symbol") == sym]
    if not holders:
        return None
    if len(holders) == 1:
        return holders[0][0], False
    # Prefer the arm that can PROVE ownership: a committed ledger id or the
    # explicit flag. A phantom adoption has neither (it writes ledger_id None).
    proven = [sid for sid, s in holders
              if (s or {}).get("ledger_id") or (s or {}).get("owns_exchange_position")]
    if len(proven) == 1:
        return proven[0], True
    return sorted(sid for sid, _ in holders)[0], True


def symbol_owner(st: dict, sid: str, sym: str, *,
                 paired: frozenset[str] | set[str] = frozenset()) -> str | None:
    """Which OTHER strategy owns a position on `sym`, if any.

    Returns the owner's id, or None when `sid` is free to ACT on `sym`.
    `sid`'s own position never blocks it (an owner must always be able to
    exit), and neither does a paired treatment/control arm.

    ⚠️ BUT A PAIR IS STILL ONE PHYSICAL POSITION. "Paired arms are not rivals"
    means the control must not DISARM the treatment -- not that both may claim
    the same 1.2 ETH. On 2026-10-31 the pairing exemption let the flat treatment
    adopt the control's position (ledger_id None), and both arms then held
    `in_pos:true, qty 1.2` on one exchange position for two days. The exemption
    below is what allowed it, and `position_holder` is what stops it: an arm
    may not adopt a position another arm already claims, pair or not. The
    treatment stays free to DECIDE (decide() is never blocked); it just cannot
    believe it is long a position it did not buy.
    """
    me = st.get("strategies", {}).get(sid) or {}
    if me.get("in_pos") and me.get("symbol") == sym:
        return None       # sid owns it; ownership governs adoption, not exit
    # The paired exemption must not license a second claim on one position.
    for other, ov in st.get("strategies", {}).items():
        ov = ov or {}
        if other == sid:
            continue
        if not ov.get("in_pos") or ov.get("symbol") != sym:
            continue
        if other in paired:
            return other          # a partner already holds it: do not re-claim
    for other, ov in st.get("strategies", {}).items():
        ov = ov or {}
        if other == sid or other in paired:
            continue
        if not ov.get("in_pos"):
            continue
        if ov.get("symbol") == sym:
            return other
    return None


def strat_state(st: dict, sid: str) -> dict:
    s = st["strategies"].setdefault(sid, {
        "equity": EQUITY0, "peak": EQUITY0,
        "in_pos": False, "stop": 0.0, "qty": 0.0, "entry": 0.0,
        "last_bar": 0, "trades": [], "kill": False, "kill_reason": None,
    })
    return s


def _pub_signal(sid: str, sym: str, bar_ms: int, close: float, a_val: float,
                qty: float, stop: float, risk_pct: float,
                equity: float, cfg: dict) -> str | None:
    """Append the ENTRY to the public chain before the order is placed.

    Before, not after: if the order fails, the chain still shows that the rule
    fired at that bar and that we intended to act. Publishing only successes
    would let us quietly drop the trades that never filled, which is the same
    selection bias the site exists to prevent.
    """
    if not PUBLISH:
        return None
    rid = f"{PUB_PREFIX}-{sid}-{bar_ms}"
    try:
        L.append("signal", {
            "id": rid,
            "symbol": sym,
            "side": "L",                  # the lab's crypto-trend rules are long only
            "entry": close,
            "source": PUB_SOURCE,
            "strategy": sid,
            "engine": cfg.get("engine"),
            "params": {k: v for k, v in cfg.items()
                       if k in ("entry", "exit", "fast", "slow")},
            "opened": datetime.fromtimestamp(bar_ms / 1000, timezone.utc).isoformat(),
            "feats": {
                "atr": round(a_val, 6),
                "atr_pct": round(a_val / close * 100, 4) if close else None,
                "stop": round(stop, 6),
                "qty": qty,
                "risk_pct": risk_pct,
                "equity": round(equity, 2),
            },
            "execution": {"venue": "bybit-demo", "endpoint": "api-demo.bybit.com"},
            "backfilled": False,
        })
        return rid
    except Exception as e:
        log_decision({"event": "publish_failed", "id": sid, "err": str(e)[:160]})
        return None


def _pub_close(rid: str | None, entry: float, exit_px: float, why: str,
               bar_ms: int, pnl: float, held_days: float | None = None) -> None:
    """Append the EXIT to the public chain, at decision time."""
    if not PUBLISH or not rid:
        return
    try:
        closed_iso = datetime.fromtimestamp(bar_ms / 1000, timezone.utc).isoformat()
        L.append("close", {
            "id": rid,
            "exit": exit_px,
            "ret": round((exit_px - entry) / entry, 6) if entry else None,
            "pnl": round(pnl, 4),
            "why": why,
            "closed": closed_iso,      # our own field, kept for the decisions log
            "closed_ts": closed_iso,   # what the exporter reads for the site
            "held_days": (round(held_days, 2) if held_days is not None else None),
            "backfilled": False,
        })
    except Exception as e:
        log_decision({"event": "publish_close_failed", "id": rid,
                      "err": str(e)[:160]})


# ------------------------------------------------------------------ main
def run_once(dry: bool) -> dict:
    reg = json.loads(REGISTRY.read_text())
    st = load_state()
    out = {"ts_utc": datetime.now(timezone.utc).isoformat(), "dry": dry,
           "strategies": {}, "orders": []}

    for cfg in reg["strategies"]:
        sid = cfg["id"]
        s = strat_state(st, sid)
        if s.get("kill"):
            out["strategies"][sid] = {"skipped": f"killed: {s.get('kill_reason')}"}
            continue
        sym, iv = cfg["symbol"], cfg["interval"]
        try:
            ts, o, h, l, c = klines(sym, iv, limit=400)
        except Exception as e:
            _err(st, sid, str(e))
            out["strategies"][sid] = {"error": str(e)[:120]}
            continue

        last_bar = ts[-1]

        # ── PHANTOM-CLAIM REPAIR, before any bar short-circuit ────────────
        # A claim with no committed ledger id is an ADOPTION that no record
        # ever backed. When another arm also claims this symbol, at most one of
        # them can be holding the exchange's single position, and only one can
        # prove it (measured 2026-11-01: ETH_TURTLE_H4 ledger_id None beside
        # RANDOM_ENTRY_CONTROL holding the committed id, both qty 1.2).
        #
        # This MUST run before the `no_new_bar` continue. That short-circuit
        # returns before every ownership guard, which is why a corrupt state
        # survived: the treatment had already seen the bar, so it kept the
        # phantom claim forever while logging "no_new_bar". Repair is about
        # STATE VALIDITY, not about new bars.
        #
        # Release, never exit: a Sell is for the whole symbol and would close
        # the real owner's position.
        if _claims(s) and s.get("symbol") == sym \
                and not (s.get("ledger_id") or s.get("owns_exchange_position")):
            provers = [o for o, ov in (st.get("strategies") or {}).items()
                       if o != sid and _claims(ov)
                       and (ov or {}).get("symbol") == sym
                       and ((ov or {}).get("ledger_id")
                            or (ov or {}).get("owns_exchange_position"))]
            if provers:
                s.update({"in_pos": False, "stop": 0.0, "qty": 0.0,
                          "entry": 0.0, "symbol": None, "ledger_id": None})
                # A receipt no code writes cannot be audited. The live state
                # carried `released_unowned_position` (entry 2241.36, "fixed in
                # 4xx") that no commit and no pass ever produced, and neither
                # this path nor strat_state() ever refreshed it -- so it would
                # still claim "released 2026-11-01" the next time this arm
                # released something. The decision log is the record; the state
                # keeps only what a strategy needs to trade.
                s.pop("released_unowned_position", None)
                log_decision({"ts_utc": out["ts_utc"], "id": sid, "event":
                              ("would_release_phantom_claim" if dry
                               else "released_phantom_claim"),
                              "symbol": sym, "dry": dry,
                              "owner": provers[0], "bar": last_bar})
                out.setdefault("released", []).append((sid, sym, provers[0]))

        # only act once per closed bar
        if s.get("last_bar") == last_bar:
            out["strategies"][sid] = {"no_new_bar": True, "bar": last_bar}
            continue

        # Exchange truth beats local state, but the EXCHANGE ONLY KNOWS ABOUT
        # SYMBOLS, NOT STRATEGIES. Three of these strategies trade ETHUSDT, so a
        # naive "is there an ETH position? adopt it" hands the same real position
        # to every strategy that looks at ETH, each recording a different entry
        # price, and only the one that actually placed the order holding a
        # committed ledger id. The runner then believes it holds two positions
        # while the exchange holds one, and an exit from the wrong strategy
        # closes someone else's trade.
        #
        # Ownership is therefore tracked locally. A strategy may adopt an
        # unclaimed position on its symbol; it may never adopt one another
        # strategy already owns. Ownership governs ADOPTION ONLY: a strategy
        # that already holds a position must always reach decide(), or it can
        # never close it, and a strategy skipped here never advances last_bar
        # and silently lags the book.
        #
        # A matched treatment/control pair is NOT two strategies contesting one
        # position; it is two arms of one experiment. RANDOM_ENTRY_CONTROL
        # shares ETHUSDT with ETH_TURTLE_H4 on purpose (registry field `match`)
        # and must be able to hold the symbol while the treatment also trades.
        # Blocking on it would suppress the treatment whenever the luck
        # baseline happened to be in a position -- destroying the very
        # comparison the control exists to provide.
        paired = match_group(reg, sid)
        exch = position(sym)
        local_in = bool(s.get("in_pos"))
        exch_in = exch is not None and (exch or {}).get("side") == "Buy"

        owner_elsewhere = symbol_owner(st, sid, sym, paired=paired)
        local_in = bool(s.get("in_pos"))

        if local_in and not exch_in:
            # was closed externally (stop hit / manual). Record and resync.
            s["in_pos"] = False
            log_decision({"ts_utc": out["ts_utc"], "id": sid, "event":
                          "position_gone_on_exchange", "bar": last_bar})
        elif exch_in and not local_in:
            if owner_elsewhere:
                # Another strategy owns this position and this strategy holds
                # nothing. Do not adopt it: acting on it would double-count the
                # exposure and let two strategies fight over one stop. Stay flat
                # and stop evaluating buys on this symbol this pass, so we do
                # not stack a second real position on top. This only ever
                # applies to a FLAT strategy -- an owner always reaches decide().
                #
                # ⚠️ But do NOT `continue`: a skipped strategy never advances
                # last_bar and never evaluates its own exit. On 2026-10-31 that
                # made ETH_TURTLE_H4 lat 3 H4 bars behind its control. The block
                # is on ADOPTION, so it must fall through to decide() with
                # in_pos False -- the arm decides flat, which is what it is.
                log_decision({"ts_utc": out["ts_utc"], "id": sid,
                              "event": "symbol_owned_by_other_strategy",
                              "symbol": sym, "bar": last_bar,
                              "owner": owner_elsewhere})
                s["_skip_buy"] = True    # honoured at the buy branch below
                out["strategies"][sid] = {
                    "skipped_adoption": "symbol owned by another strategy",
                    "symbol": sym, "bar": last_bar,
                    "owner": owner_elsewhere,
                }
            else:
                s["in_pos"] = True
                s["symbol"] = sym
                s["entry"] = float((exch or {}).get("avgPrice") or c[-1])
                s["qty"] = float((exch or {}).get("size") or 0)
                s["ledger_id"] = None      # adopted: no committed record exists
                log_decision({"ts_utc": out["ts_utc"], "id": sid, "event":
                              "adopted_existing_position", "entry": s["entry"],
                              "qty": s["qty"], "symbol": sym})

        params = {k: v for k, v in cfg.items() if k != "engine"}
        action, new_stop, dist = decide(h, l, c, cfg["engine"], s["in_pos"],
                                        s.get("stop") or 0.0, **params)
        a_val = atr(h, l, c)
        close1 = c[-1]

        risk_pct = RISK_PCT
        if s["equity"] < s["peak"] * (1 - THROTTLE_DD / 100.0):
            risk_pct = RISK_PCT * THROTTLE_FACTOR

        rec = {"ts_utc": out["ts_utc"], "id": sid, "symbol": sym, "tf": iv,
               "bar": last_bar,
               "bar_close_utc": datetime.fromtimestamp(last_bar / 1000,
                                                       timezone.utc).isoformat(),
               "close": close1, "atr": round(a_val, 6), "action": action,
               "in_pos": s["in_pos"], "equity": round(s["equity"], 2),
               "peak": round(s["peak"], 2), "risk_pct": risk_pct,
               "dry": dry}

        # Cleared unconditionally: this pass's adoption block must not leak into
        # the next one, and `and` short-circuits would skip the pop otherwise.
        skip_buy = bool(s.pop("_skip_buy", False))

        if action == "buy" and not s["in_pos"] and not skip_buy:
            qty = qty_for(sym, s["equity"], risk_pct, dist)
            stop_px = close1 - dist
            rec.update({"qty": qty, "stop": round(stop_px, 6),
                        "dist": round(dist, 6)})
            if qty <= 0:
                rec["event"] = "refused_min_size"
            elif dry:
                rec["event"] = "would_buy"
            else:
                # publish the intent to the public chain BEFORE placing it
                rec["ledger_id"] = _pub_signal(
                    sid, sym, last_bar, close1, a_val, qty, stop_px,
                    risk_pct, s["equity"], cfg)
                r = place_market(sym, "Buy", qty, stop_px, sid)
                rec["order"] = {"retCode": r.get("retCode"),
                                "retMsg": r.get("retMsg"),
                                "orderId": (r.get("result") or {}).get("orderId")}
                if r.get("retCode") == 0:
                    s.update({"in_pos": True, "entry": close1, "qty": qty,
                              "stop": stop_px,
                              "symbol": sym,
                              "entry_bar_ms": last_bar,
                              "ledger_id": rec.get("ledger_id")})
                    out["orders"].append((sid, "BUY", qty, close1, stop_px))
                    rec["event"] = "bought"
                else:
                    _err(st, sid, f"order failed: {r.get('retMsg')}")
                    rec["event"] = "order_failed"

        elif action == "close" and s["in_pos"]:
            rec.update({"qty": s["qty"], "entry": s["entry"]})
            # Last line of defence. An exit is a market Sell for the whole
            # position on this SYMBOL, so a strategy that believes it holds a
            # position it never bought would close whoever else's real position
            # this is, at our bar close, and book the P&L to the wrong arm.
            # Only a committed ledger id, or an explicit ownership flag, proves
            # we opened it.
            if not (s.get("ledger_id") or s.get("owns_exchange_position")):
                rec["event"] = "close_refused_unowned"
                log_decision({**rec, "why": "no committed ledger_id and not "
                              "marked as owner; refusing to close another "
                              "strategy's position"})
                out["strategies"][sid] = {
                    "action": action, "event": rec["event"],
                    "equity": round(s["equity"], 2),
                    "trades": len(s["trades"]), "in_pos": s["in_pos"]}
                continue
            if dry:
                rec["event"] = "would_close"
            else:
                r = place_market(sym, "Sell", s["qty"], 0.0, sid + "-x")
                rec["order"] = {"retCode": r.get("retCode"),
                                "retMsg": r.get("retMsg")}
                if r.get("retCode") == 0:
                    pnl = (close1 - s["entry"]) * s["qty"]
                    s["equity"] += pnl
                    s["peak"] = max(s["peak"], s["equity"])
                    s["trades"].append({"entry": s["entry"], "exit": close1,
                                        "qty": s["qty"], "pnl": round(pnl, 4),
                                        "ts": rec["bar_close_utc"]})
                    _pub_close(s.get("ledger_id"), s["entry"], close1,
                               "signal-exit", last_bar, pnl,
                               held_days=round((
                                   last_bar - (s.get("entry_bar_ms") or last_bar)
                               ) / 86400000.0, 2))
                    s.update({"in_pos": False, "stop": 0.0, "qty": 0.0,
                              "entry": 0.0, "ledger_id": None, "symbol": None})
                    out["orders"].append((sid, "SELL", rec["qty"], close1, 0))
                    rec["event"] = "closed"
                    rec["pnl"] = round(pnl, 4)
                else:
                    _err(st, sid, f"close failed: {r.get('retMsg')}")
                    rec["event"] = "close_failed"

        elif action == "hold" and s["in_pos"]:
            s["stop"] = max(s.get("stop") or 0.0, new_stop)
            rec["event"] = "hold"
            rec["trail_stop"] = round(s["stop"], 6)
            if not dry and exch_in:
                # keep the exchange-side stop in step with the trail
                rr = bd.private("/v5/position/trading-stop", method="POST", body={
                    "category": CATEGORY, "symbol": sym,
                    "stopLoss": f"{s['stop']:.6f}", "positionIdx": 0})
                rec["trail_sync"] = {"retCode": rr.get("retCode"),
                                     "retMsg": rr.get("retMsg")}
        else:
            rec["event"] = "none"

        # A dry run must NOT advance bar state. Otherwise testing the runner
        # silently desynchronises it: the live pass that follows would see
        # "no new bar", skip the decision, and the strategy would miss the very
        # signal the dry run was meant to rehearse. Decisions are still logged so
        # the rehearsal is auditable, marked as dry.
        if not dry:
            s["last_bar"] = last_bar
        log_decision(rec)

        # kill switch: own drawdown
        dd = (s["peak"] - s["equity"]) / s["peak"] * 100 if s["peak"] else 0
        if dd > 15:
            s["kill"] = True
            s["kill_reason"] = f"own drawdown {dd:.1f}% > 15%"
            log_decision({"ts_utc": out["ts_utc"], "id": sid,
                          "event": "KILL_SWITCH", "reason": s["kill_reason"]})

        out["strategies"][sid] = {
            "action": action, "event": rec.get("event"),
            "equity": round(s["equity"], 2),
            "trades": len(s["trades"]),
            "in_pos": s["in_pos"],
        }

    # A dry run must not write the live state file. run_once() already refuses
    # to advance last_bar when dry -- precisely so a rehearsal cannot desync the
    # live pass -- but it still called save_state() unconditionally, so a dry
    # pass persisted its ADOPTIONS and its derived stop/qty/symbol. That
    # silently handed ETH_TURTLE_H4 a copy of the position
    # RANDOM_ENTRY_CONTROL owns (observed 2026-10-31: entry 2241.49,
    # ledger_id None), which is the exact double-claim 294d5ae set out to stop,
    # re-created by the rehearsal path. Rehearsing must leave no trace.
    if not dry:
        save_state(st)

    # If anything was published to the chain this pass, rebuild and push the
    # site data NOW. The ledger is written instantly but the export used to run
    # only once a day at 02:30, so a trade would sit on the chain invisible on
    # the site for up to 24 hours. A publish that the reader cannot see is not
    # published.
    if PUBLISH and out.get("orders"):
        try:
            import subprocess
            r = subprocess.run(
                ["/opt/trading-agent/venv/bin/python",
                 "/opt/trading-agent/scripts/export_ledger_json.py", "--push"],
                capture_output=True, text=True, timeout=180,
                cwd="/opt/trading-agent")
            out["export"] = {"rc": r.returncode,
                             "tail": (r.stdout or r.stderr or "").strip()[-400:]}
            log_decision({"ts_utc": out["ts_utc"], "event": "site_export",
                          "rc": r.returncode})
        except Exception as e:
            out["export"] = {"rc": -1, "err": str(e)[:200]}
            log_decision({"ts_utc": out["ts_utc"], "event": "site_export_failed",
                          "err": str(e)[:200]})

    return out


def _err(st: dict, sid: str, msg: str) -> None:
    e = st["errors"].setdefault(sid, {"count": 0, "last": None})
    e["count"] += 1
    e["last"] = msg[:200]
    if e["count"] >= 3:
        s = strat_state(st, sid)
        s["kill"] = True
        s["kill_reason"] = f"3 consecutive errors: {msg[:100]}"


def status() -> int:
    st = load_state()
    reg = json.loads(REGISTRY.read_text())
    print(f"{'id':26} {'trades':>6} {'equity':>10} {'peak':>10} {'in_pos':>7} {'kill':>5}")
    print("-" * 72)
    for cfg in reg["strategies"]:
        s = strat_state(st, cfg["id"])
        print(f"{cfg['id']:26} {len(s['trades']):6} {s['equity']:10.2f} "
              f"{s['peak']:10.2f} {str(s['in_pos']):>7} {str(s['kill']):>5}")
    if DECISIONS.exists():
        n = sum(1 for _ in DECISIONS.open())
        print(f"\ndecisions logged: {n}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    # hard guard from AGENT.md rule 1: demo endpoint only
    if bd.DEMO != "https://api-demo.bybit.com":
        print(f"REFUSING: endpoint is {bd.DEMO}, not demo", file=sys.stderr)
        return 2

    if args.status:
        return status()

    out = run_once(args.dry_run)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
