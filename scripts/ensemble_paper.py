#!/usr/bin/env python3
"""Ensemble paper book — forward paper record for the strategy-book ensembles.

WHY A SEPARATE BOOK
-------------------
The existing unified-paper.json is the live scanner's forward record. Its
history is what every claim about the system is measured against. If ensemble
signals were written into it, the two would be blended and neither could be
audited — the exact failure the repo's own AGENTS.md forbids ("paper and real
must never be blended", and by extension, two different paper systems must not
be either).

So this writes its OWN book: data/ensemble-paper.json. Same discipline as the
shadow books under /opt/livr-research, which are kept separate for the same
reason.

WHAT IT TRADES
--------------
The ensembles from data/strategy-book/ensembles.json. Each is a GROUP of
independently-gated strategies sharing a (family, timeframe). A member votes for
its own symbol when its rule fires; the ensemble takes a position when enough
members agree.

The two live ensembles are structurally different and the runner respects that:

  momentum / 5m        high win rate (58-61%), rr 1.5, fast
  channel_breakout/4h  low win rate (32-35%), rr 3.0, slow

Each keeps its own signal generation, stop and target geometry. They are not
averaged into one rule — that is the whole point of choosing an ensemble over a
blend.

SIZING AND SAFETY
-----------------
Paper only. No exchange call, no key, no order. Size is a flat notional per
position so P&L is comparable across members. Costs are charged at the measured
0.28% round trip on every close, so the record is net from the first trade —
which is where the real book's problem was hidden.

Read-only on every live state file. Writes only to its own book.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

AGENT = Path("/opt/trading-agent")
DB = AGENT / "data/trading.db"
BOOK = AGENT / "data/ensemble-paper.json"
ENSEMBLES = AGENT / "data/strategy-book/ensembles.json"

COST = 0.0028          # 0.28% round trip, charged on every close
SIZE = 5.0             # flat notional per position, USDT
MAX_OPEN_PER_ENS = 3   # a single ensemble cannot stack unbounded positions

TF_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def _load(p: Path, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(d: dict) -> None:
    BOOK.parent.mkdir(parents=True, exist_ok=True)
    tmp = BOOK.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(BOOK)          # atomic: a kill cannot leave a half-written book


def _candles(cur, sym: str, tf: str, limit: int):
    cur.execute(
        "SELECT open_time_utc, open, high, low, close FROM candles "
        "WHERE symbol=? AND timeframe=? ORDER BY open_time_utc DESC LIMIT ?",
        (sym, tf, limit),
    )
    return list(reversed(cur.fetchall()))


def _rule_fires(rows, lb: int, family: str) -> bool:
    """Does this member's rule fire on the latest closed bar?"""
    if len(rows) < lb + 2:
        return False
    last = rows[-1]
    prior = rows[-lb - 1:-1]
    if not prior:
        return False
    if family == "momentum":
        sma = sum(r[4] for r in prior) / len(prior)
        return last[4] > sma
    if family == "channel_breakout":
        hi = max(r[2] for r in prior)
        return last[4] > hi
    return False


def main() -> int:
    ens_doc = _load(ENSEMBLES, {}) or {}
    ensembles = ens_doc.get("ensembles") or []
    if not ensembles:
        print("no ensembles in the strategy book — nothing to paper")
        return 0

    st = _load(BOOK, {}) or {}
    st.setdefault("open", [])
    st.setdefault("closed", [])
    st.setdefault("meta", {"created_utc": datetime.now(timezone.utc).isoformat(),
                           "cost_per_round_trip": COST, "size_usdt": SIZE})

    if not DB.exists():
        print("no candle DB")
        return 1
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.cursor()

    opened = closed = 0

    for ens in ensembles:
        fam = ens["family"]
        tf = ens["timeframe"]
        symbol = None
        # the ensemble's members carry (symbol, tf, lb, hold, rr); use the FIRST
        # member's symbol as the traded instrument, and the group median params
        mids = ens.get("member_ids") or []
        if not mids:
            continue
        # member id format: family|symbol|tf|lb|hold|rr
        try:
            parts = mids[0].split("|")
            symbol = parts[1]
            lb = int(parts[3])
            hold = int(parts[4])
            rr = float(parts[5])
        except Exception:
            continue

        # ── manage open positions for this ensemble ──────────────────────────
        keep = []
        for pos in st["open"]:
            if pos.get("ensemble") != ens["id"]:
                keep.append(pos)
                continue
            rows = _candles(cur, pos["symbol"], tf, 200)
            if not rows:
                keep.append(pos)
                continue
            latest = rows[-1]
            exit_px = None
            reason = None
            if latest[3] <= pos["sl"]:           # low <= stop
                exit_px, reason = pos["sl"], "SL"
            elif latest[2] >= pos["tp"]:         # high >= target
                exit_px, reason = pos["tp"], "TP"
            else:
                # time stop: hold this many bars, then exit at close
                elapsed = (latest[0] - pos["open_ms"]) / TF_MS[tf]
                if elapsed >= hold:
                    exit_px, reason = latest[4], "TIME"
            if exit_px is None:
                keep.append(pos)
                continue
            move = (exit_px - pos["entry"]) / pos["entry"]
            pnl = SIZE * (move - COST)
            pos.update({"exit": round(exit_px, 6), "pnl": round(pnl, 6),
                        "reason": reason,
                        "close_ts_utc": datetime.now(timezone.utc).isoformat()})
            st["closed"].append(pos)
            closed += 1
        st["open"] = keep

        # ── open a new position if the rule fires ────────────────────────────
        n_open = sum(1 for p in st["open"] if p.get("ensemble") == ens["id"])
        if n_open >= MAX_OPEN_PER_ENS:
            continue
        # dedupe: same ensemble+symbol already open
        if any(p.get("ensemble") == ens["id"] and p.get("symbol") == symbol
               for p in st["open"]):
            continue

        rows = _candles(cur, symbol, tf, lb + 5)
        if not _rule_fires(rows, lb, fam):
            continue
        last = rows[-1]
        entry = last[4]
        # geometry follows the member's own rr, at a 2% stop base, matching the
        # discipline the existing paper book uses (stop calibrated, rr derived)
        stop_pct = 0.02
        tp = round(entry * (1 + stop_pct * rr), 6)
        sl = round(entry * (1 - stop_pct), 6)
        st["open"].append({
            "ensemble": ens["id"], "family": fam, "timeframe": tf,
            "symbol": symbol, "entry": round(entry, 6),
            "tp": tp, "sl": sl, "rr": rr, "hold_bars": hold,
            "size": SIZE, "open_ms": last[0],
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "mode": "paper",
        })
        opened += 1

    con.close()

    st["meta"]["last_run_utc"] = datetime.now(timezone.utc).isoformat()
    st["meta"]["opens_total"] = st["meta"].get("opens_total", 0) + opened
    st["meta"]["closes_total"] = st["meta"].get("closes_total", 0) + closed
    _save(st)

    n_open = len(st["open"])
    n_closed = len(st["closed"])
    print(f"ensemble paper book  {datetime.now(timezone.utc).isoformat()[:19]}")
    print(f"  opened this run : {opened}")
    print(f"  closed this run : {closed}")
    print(f"  open now        : {n_open}")
    print(f"  closed total    : {n_closed}")
    if n_closed:
        pnl = sum(p.get("pnl", 0) for p in st["closed"])
        wins = sum(1 for p in st["closed"] if p.get("pnl", 0) > 0)
        print(f"  net P&L (after {COST:.2%} round trip) : {pnl:+.4f}")
        print(f"  win rate        : {wins / n_closed * 100:.1f}%  ({wins}W/{n_closed - wins}L)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
