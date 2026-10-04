#!/usr/bin/env python3
"""The strategy book — a ledger of every candidate strategy and its own record.

WHAT THIS IS
------------
The owner's ask: "create a strategy book expecting thousands of small strategies
in a few days, then compact them into a multifunctional strategy."

This is the book. It is deliberately modelled on the discipline the rest of this
repo already holds itself to: every entry has its OWN record, and nothing is
promoted on a number that cannot be traced back to that record. A thousand
strategies whose individual results cannot be audited are worse than useless —
they are a machine for making noise look like signal.

THE PIPELINE (owner's design)
-----------------------------
    SCAN       every 15m   cheap, observational
    GENERATE   every 15m   propose candidates from a legible parameter space
    VALIDATE   on ingest   full-history split gate, cost-realistic
    BOOK       here        keep every result, pass or fail, with its numbers
    COMPACT    later       cluster survivors into an ensemble, not a blend

WHY EVERY FAILURE IS KEPT
-------------------------
The rejected candidates are the most honest data in the book. They are the
record of what was tried and did not work, which is exactly what stops the same
bad idea being re-proposed forever. A book that only lists winners is a marketing
document, not a ledger.

STORAGE
-------
Two append-only jsonl files plus a compact index, so the book can grow to
hundreds of thousands of entries without a database:

    data/strategy-book/entries.jsonl    one line per evaluated strategy
    data/strategy-book/index.json       rolled-up counts, per-symbol tallies
    data/strategy-book/ensembles.json   compacted groups (written by compact.py)

Read-only on all trading state. This file places no orders and edits no
parameters.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

AGENT = Path("/opt/trading-agent")
BOOK = AGENT / "data/strategy-book"
ENTRIES = BOOK / "entries.jsonl"
INDEX = BOOK / "index.json"
DB = AGENT / "data/trading.db"

COST = 0.0028           # 0.28% round trip, measured
MIN_TRADES = 60         # per half; a split result below this is not evidence
UNIVERSE = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
            "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOGE/USDT"]

# which timeframes to mine. Lower ones give far more samples, which is the
# whole reason the history was scraped down to 5m.
TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h"]

TF_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def _load(p: Path, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _ids() -> set:
    out = set()
    try:
        with open(ENTRIES, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    out.add(json.loads(line).get("id", ""))
    except Exception:
        pass
    return out


def _append(rec: dict) -> None:
    BOOK.mkdir(parents=True, exist_ok=True)
    with open(ENTRIES, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def candles(cur, sym: str, tf: str):
    cur.execute(
        "SELECT open_time_utc, open, high, low, close, volume FROM candles "
        "WHERE symbol=? AND timeframe=? ORDER BY open_time_utc",
        (sym, tf),
    )
    return cur.fetchall()


# ── the strategy family ──────────────────────────────────────────────────────
# A legible space, not arbitrary generation. Every candidate is describable in
# one sentence, which is what makes the book auditable.

def channel_breakout(rows, lb: int, hold: int, rr: float):
    n = wins = 0
    net = 0.0
    for i in range(lb + 1, len(rows) - 1):
        hi = max(r[2] for r in rows[i - lb:i])
        if rows[i][4] <= hi:
            continue
        entry = rows[i][4]
        stop = entry * 0.98
        target = entry * (1 + 0.02 * rr)
        for j in range(i + 1, min(i + 1 + hold, len(rows))):
            _t, _o, h, l, _c, _v = rows[j]
            if l <= stop:
                net += (stop - entry) / entry - COST
                n += 1
                break
            if h >= target:
                net += (target - entry) / entry - COST
                n += 1
                wins += 1
                break
    return n, wins, net


def mean_reversion(rows, lb: int, hold: int, rr: float):
    """Buy a dip below the prior channel low, target a return to the mean."""
    n = wins = 0
    net = 0.0
    for i in range(lb + 1, len(rows) - 1):
        lo = min(r[3] for r in rows[i - lb:i])
        if rows[i][4] >= lo:
            continue
        entry = rows[i][4]
        stop = entry * 0.98
        target = entry * (1 + 0.02 * rr)
        for j in range(i + 1, min(i + 1 + hold, len(rows))):
            _t, _o, h, l, _c, _v = rows[j]
            if l <= stop:
                net += (stop - entry) / entry - COST
                n += 1
                break
            if h >= target:
                net += (target - entry) / entry - COST
                n += 1
                wins += 1
                break
    return n, wins, net


def momentum(rows, lb: int, hold: int, rr: float):
    """Buy when the close is above the SMA of the prior lb bars."""
    n = wins = 0
    net = 0.0
    for i in range(lb + 1, len(rows) - 1):
        sma = sum(r[4] for r in rows[i - lb:i]) / lb
        if rows[i][4] <= sma:
            continue
        entry = rows[i][4]
        stop = entry * 0.98
        target = entry * (1 + 0.02 * rr)
        for j in range(i + 1, min(i + 1 + hold, len(rows))):
            _t, _o, h, l, _c, _v = rows[j]
            if l <= stop:
                net += (stop - entry) / entry - COST
                n += 1
                break
            if h >= target:
                net += (target - entry) / entry - COST
                n += 1
                wins += 1
                break
    return n, wins, net


FAMILIES = {"channel_breakout": channel_breakout,
            "mean_reversion": mean_reversion,
            "momentum": momentum}


def gate(rows, fn, lb: int, hold: int, rr: float):
    """Full-history split gate. Both halves must clear MIN_TRADES and be positive."""
    half = len(rows) // 2
    a, b = rows[:half], rows[half:]
    na, wa, neta = fn(a, lb, hold, rr)
    nb, wb, netb = fn(b, lb, hold, rr)
    if na < MIN_TRADES or nb < MIN_TRADES:
        return None, f"sample too small (first {na}, second {nb} < {MIN_TRADES})"
    if neta <= 0 or netb <= 0:
        return None, f"failed split (first {neta:+.4f}, second {netb:+.4f})"
    n, w, net = fn(rows, lb, hold, rr)
    if n < 2 * MIN_TRADES:
        return None, f"full-sample too small ({n})"
    return {"n": n, "wins": w, "net": round(net, 4),
            "wr": round(w / n * 100, 2),
            "net_per_trade": round(net / n, 6),
            "first_net": round(neta, 4), "second_net": round(netb, 4),
            "first_n": na, "second_n": nb}, None


def main() -> int:
    quiet = "--quiet" in sys.argv
    limit_tf = None
    if "--tf" in sys.argv:
        limit_tf = sys.argv[sys.argv.index("--tf") + 1].split(",")

    if not DB.exists():
        print("no candle DB")
        return 1

    BOOK.mkdir(parents=True, exist_ok=True)
    seen = _ids()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.cursor()

    tfs = [t for t in TIMEFRAMES if (limit_tf is None or t in limit_tf)]
    added = passed = failed = 0
    by_tf = Counter()
    by_family = Counter()
    by_symbol = Counter()

    for tf in tfs:
        for sym in UNIVERSE:
            rows = candles(cur, sym, tf)
            if len(rows) < 4 * MIN_TRADES:
                continue
            for fname, fn in FAMILIES.items():
                for lb in (12, 24, 48, 96):
                    for hold in (12, 24, 48):
                        for rr in (1.5, 2.0, 3.0):
                            cid = f"{fname}|{sym}|{tf}|{lb}|{hold}|{rr}"
                            if cid in seen:
                                continue
                            res, why = gate(rows, fn, lb, hold, rr)
                            added += 1
                            if res is None:
                                failed += 1
                                _append({"id": cid, "ts_utc": datetime.now(timezone.utc).isoformat(),
                                         "family": fname, "symbol": sym, "timeframe": tf,
                                         "lb": lb, "hold": hold, "rr": rr,
                                         "status": "rejected", "reason": why})
                                continue
                            passed += 1
                            by_tf[tf] += 1
                            by_family[fname] += 1
                            by_symbol[sym] += 1
                            _append({"id": cid, "ts_utc": datetime.now(timezone.utc).isoformat(),
                                     "family": fname, "symbol": sym, "timeframe": tf,
                                     "lb": lb, "hold": hold, "rr": rr,
                                     "status": "passed", **res})

    con.close()

    idx = _load(INDEX, {}) or {}
    idx["as_of_utc"] = datetime.now(timezone.utc).isoformat()
    idx["total_entries"] = (idx.get("total_entries", 0) or 0) + added
    idx["total_passed"] = (idx.get("total_passed", 0) or 0) + passed
    idx["total_rejected"] = (idx.get("total_rejected", 0) or 0) + failed
    idx["passed_by_timeframe"] = dict(by_tf)
    idx["passed_by_family"] = dict(by_family)
    idx["passed_by_symbol"] = dict(by_symbol)
    with open(INDEX, "w", encoding="utf-8") as f:
        json.dump(idx, f, indent=2)

    if not quiet:
        print(f"strategy book run  {datetime.now(timezone.utc).isoformat()[:19]}")
        print(f"  evaluated : {added}")
        print(f"  passed    : {passed}")
        print(f"  rejected  : {failed}")
        if by_tf:
            print("  passed by timeframe:")
            for k, v in sorted(by_tf.items()):
                print(f"    {k:5} {v}")
        if by_family:
            print("  passed by family:")
            for k, v in by_family.most_common():
                print(f"    {k:18} {v}")
        if not passed:
            print("  nothing passed the gate this run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
