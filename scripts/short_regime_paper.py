#!/usr/bin/env python3
"""Regime-gated short paper lane — forward record for the certified-nothing rules.

WHY THIS LANE EXISTS
--------------------
The six short-breakdown rules from the standing hunt were certified today and
NONE of them passed: every one loses in the same fold, and that fold was the
sample's largest rally (BTC +104%). They are not broken — they are short-only, and
a short rule loses when the market rises. See
docs/proposals/short-certification-verdict-2026-10-24.md.

One configuration separates the losing period from the profitable one cleanly:
enable the shorts only when BTC is BELOW its 200-bar SMA. In the walk-forward that
gate was ON in exactly one fold (+$7.50 caught) and OFF in the fold that lost
-$10.80 (avoided).

That is n=1 observed regime. A hypothesis with one confirming observation is not a
result, and this lane exists to find out rather than assume. It costs nothing and
records every decision so the answer arrives in months rather than another
backtest.

WHY ITS OWN BOOK
----------------
data/unified-paper.json is the live scanner's forward record and is what every
claim about the system is measured against. Writing these signals into it would
blend two different systems and neither could be audited. So this writes
data/short-regime-paper.json and touches nothing else.

WHAT IT DOES, PER RUN (idempotent — safe to run hourly)
-------------------------------------------------------
  1. settle any open position whose horizon has passed, at the close of its
     exit bar, charging the full round trip so the record is net from trade one
  2. read the latest CLOSED 4h bar for each symbol and evaluate each rule
  3. if BTC is below its 200-bar SMA, open a short for each firing rule
  4. record the gate state on every run, INCLUDING when the gate is shut, so the
     record shows how often the lane was eligible and not just when it traded

CLOSED MEANS CLOSED
-------------------
The `candles` table is NOT a closed-bar series. ingest_all.py (:12/:27/:42/:57)
requests raw `limit=60` candles including the in-progress one, and the store is
INSERT OR IGNORE on (symbol, timeframe, open_time_utc) — so a 4h row exists from
about fifteen minutes into its window and holds whatever the close was at that
moment, frozen. Reading bar[-1] therefore priced the gate and the entry off a
forming candle (found 2026-10-24, tests/test_short_lane_closed_bar.py).

So the lane filters what it reads: a bar is usable only once
`now >= open_time + 4h`. The gate, the entry and the settle walk all go through
`last_closed()`, so no decision in this file can see a bar that has not closed.
Cost: entries are one bar later than they were. That is the price of the entry
being a price that existed.

COSTS
-----
SIZE_USDT notional per position, COST_PCT round trip charged on close. Defaults
match the rest of the paper books so the numbers are comparable.

Read-only on every live state file. Writes only to its own book.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
sys.path.insert(0, str(BASE / "scripts"))
sys.path.insert(0, str(BASE / "src"))

DB = BASE / "data" / "trading.db"
BOOK = BASE / "data" / "short-regime-paper.json"

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
           "BNB/USDT", "ADA/USDT", "LINK/USDT", "DOGE/USDT"]
TF = "4h"
TF_MS = 4 * 3600 * 1000
# Any timeframe the lane is pointed at; used to decide when a bar has closed.
TF_MS_BY_NAME = {"5m": 5 * 60 * 1000, "30m": 30 * 60 * 1000,
                 "1h": 3600 * 1000, "4h": TF_MS, "1d": 24 * 3600 * 1000}

SIZE_USDT = 5.0
COST_PCT = 0.30 / 100          # round trip, charged on close
GATE_SMA = 200                 # BTC bars
STOP_FRAC = 0.04               # fixed in advance, same as the certification
TARGET_FRAC = 0.12
HOLD_BARS = 120
MAX_OPEN = 8


def _load(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _save(p: Path, d) -> None:
    p.write_text(json.dumps(d, indent=1, sort_keys=True))


# ----------------------------------------------------------------- the rules
# Imported from hunt5 so the lane trades exactly the rules that were certified,
# not a re-implementation that could drift from them.
import hunt5_open_ended as h5  # noqa: E402

RULES = {
    "volbreak 20 x2.0": h5.f_volbreak(20, 2.0),
    "volbreak 100 x1.5": h5.f_volbreak(100, 1.5),
    "volbreak 55 x1.5": h5.f_volbreak(55, 1.5),
    "bearcross 20/100": h5.f_bearcross(20, 100),
}


def bars(con, symbol: str, limit: int = 260):
    return con.execute(
        "select open_time_utc, open, high, low, close, volume from candles "
        "where symbol=? and timeframe=? order by open_time_utc desc limit ?",
        (symbol, TF, limit),
    ).fetchall()[::-1]


def last_closed(rows, tf: str, now_ms: int):
    """Drop every bar whose window has not ended at `now_ms`.

    The candles table carries forming bars (see the module docstring), so the
    newest row is not automatically a closed bar. A bar opened at `t` is closed
    once `now >= t + duration`; the predicate is on the OPEN time, so it holds no
    matter how late the row was written or how frozen its values are.
    """
    dur = TF_MS_BY_NAME[tf]
    return [r for r in rows if r[0] + dur <= now_ms]


class Bar:
    __slots__ = ("open_time_utc", "open", "high", "low", "close", "volume")

    def __init__(self, r):
        (self.open_time_utc, self.open, self.high, self.low,
         self.close, self.volume) = r


def gate_open(con, now: int | None = None) -> tuple[bool, dict]:
    """BTC below its 200-bar 4h SMA → shorts enabled."""
    if now is None:
        now = int(time.time() * 1000)
    b = last_closed(bars(con, "BTC/USDT", GATE_SMA + 5), TF, now)
    if len(b) < GATE_SMA:
        return False, {"reason": "insufficient BTC history", "bars": len(b)}
    closes = [r[4] for r in b][-GATE_SMA:]
    sma = sum(closes) / GATE_SMA
    px = closes[-1]
    return px < sma, {"btc_px": px, "sma200": sma,
                      "dist_pct": (px / sma - 1) * 100}


def settle(con, st: dict, now: int | None = None) -> int:
    """Close any open position whose horizon has elapsed. Returns count closed."""
    if now is None:
        now = int(time.time() * 1000)
    closed_n = 0
    still_open = []
    for pos in st["open"]:
        sym = pos["symbol"]
        b = last_closed(bars(con, sym, HOLD_BARS + 10), TF, now)
        have = {r[0] for r in b}
        exit_ts = pos["open_bar_ms"] + HOLD_BARS * TF_MS
        if exit_ts not in have:
            still_open.append(pos)               # horizon not reached yet
            continue
        # walk the bars from entry to the horizon checking stop then target
        path = [r for r in b if pos["open_bar_ms"] < r[0] <= exit_ts]
        entry = pos["entry"]
        stop_px = entry * (1 + STOP_FRAC)        # short: stop is ABOVE
        tgt_px = entry * (1 - TARGET_FRAC)       # target BELOW
        exit_px, why, ts = None, "TIME", exit_ts
        for r in path:
            if r[2] >= stop_px:                  # high hits stop first
                exit_px, why, ts = stop_px, "SL", r[0]
                break
            if r[3] <= tgt_px:                   # low hits target
                exit_px, why, ts = tgt_px, "TP", r[0]
                break
        if exit_px is None and path:
            exit_px = path[-1][4]                # last close at horizon
        if exit_px is None:
            still_open.append(pos)
            continue
        gross = (entry - exit_px) / entry * SIZE_USDT   # short P&L
        cost = SIZE_USDT * COST_PCT
        st["closed"].append({
            **{k: pos[k] for k in ("symbol", "rule", "entry", "open_ts_utc")},
            "exit": round(exit_px, 8), "why": why,
            "close_ts_utc": datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(),
            "pnl_gross": round(gross, 6),
            "pnl_net": round(gross - cost, 6),
            "gate_btc_dist_pct": pos.get("gate_btc_dist_pct"),
        })
        closed_n += 1
    st["open"] = still_open
    return closed_n


def main() -> int:
    if not DB.exists():
        print("no candle DB")
        return 1
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    now = int(time.time() * 1000)

    st = _load(BOOK, {}) or {}
    st.setdefault("open", [])
    st.setdefault("closed", [])
    st.setdefault("runs", [])
    st.setdefault("meta", {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "regime-gated short paper lane; hypothesis, n=1 at inception",
        "gate": f"BTC close < {GATE_SMA}-bar {TF} SMA",
        "size_usdt": SIZE_USDT, "cost_round_trip_pct": COST_PCT * 100,
        "exit": {"stop_frac": STOP_FRAC, "target_frac": TARGET_FRAC,
                 "hold_bars": HOLD_BARS},
        "note": ("Fixed in advance, identical to the certification run, so the "
                 "forward record cannot be tuned to the forward sample."),
    })
    seg = st["meta"].setdefault("seams", [])
    if not any(s.get("closed_bars_only") for s in seg):
        # First run after the forming-bar fix: everything in the book before this
        # mark was priced off bar[-1] whenever that row was partially formed, so
        # it is kept (never deleted) but not comparable to what follows.
        seg.append({
            "from_utc": datetime.now(timezone.utc).isoformat(),
            "closed_bars_only": True,
            "reason": ("bars are filtered to those whose 4h window has ended; "
                       "earlier runs read the newest row, which may have been a "
                       "forming bar (tests/test_short_lane_closed_bar.py)"),
        })

    closed_n = settle(con, st, now)

    is_open, gi = gate_open(con, now)
    latest = {}
    for s in SYMBOLS:
        b = last_closed(bars(con, s, 260), TF, now)
        if b:
            latest[s] = b[-1][0]

    opened = 0
    fired = []
    if is_open:
        for sym in SYMBOLS:
            b = last_closed(bars(con, sym, 260), TF, now)
            if len(b) < 55:
                continue
            bar_ms = b[-1][0]
            if any(p["symbol"] == sym for p in st["open"]):
                continue
            if any(c["symbol"] == sym and c["close_ts_utc"]
                   and c["open_ts_utc"] == latest.get(sym) for c in st["closed"]):
                continue
            objs = [Bar(r) for r in b]
            for name, fn in RULES.items():
                try:
                    hit = fn(objs)
                except Exception:
                    hit = False
                if not hit:
                    continue
                fired.append(name)
                if len(st["open"]) >= MAX_OPEN:
                    break
                st["open"].append({
                    "symbol": sym, "rule": name, "side": "short",
                    "entry": b[-1][4], "size_usdt": SIZE_USDT,
                    "open_bar_ms": bar_ms,
                    "open_ts_utc": datetime.fromtimestamp(
                        bar_ms / 1000, timezone.utc).isoformat(),
                    "gate_btc_dist_pct": round(gi.get("dist_pct", 0), 2),
                })
                opened += 1

    st["runs"].append({
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "gate_open": is_open, "gate": gi,
        "opened": opened, "closed": closed_n,
        "rules_fired": fired, "open_now": len(st["open"]),
        "latest_bar_ms": max(latest.values()) if latest else None,
    })
    st["runs"] = st["runs"][-2000:]      # keep the file bounded

    _save(BOOK, st)

    n_cl = len(st["closed"])
    net = sum(c["pnl_net"] for c in st["closed"])
    print(f"gate {'OPEN' if is_open else 'shut'} "
          f"(BTC {gi.get('dist_pct', 0):+.1f}% vs SMA200) | "
          f"opened {opened} | closed {closed_n} | "
          f"open now {len(st['open'])} | "
          f"book: {n_cl} closed, net {net:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
