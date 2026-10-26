#!/usr/bin/env python3
"""Backfill perpetual funding rates into the candle database.

WHY
---
`net_settlement.py` charges funding per 8h period crossed, but the `funding` table
did not exist, so every settled record reported funding as 0 and said so. A
backtest that silently ignores funding is wrong in a specific direction: it flatters
any strategy that holds a position long enough to pay it, which is precisely the
long-hold trend strategies being tested. The source lab's own handbook flags this
("perpetual funding ... NOT modelled in the lab, log it") — this is that log.

WHAT IT DOES
------------
Pulls the full funding history from Bybit for each symbol, stores it in a `funding`
table keyed (symbol, ts), and is idempotent: re-running only inserts what is
missing. Bybit paginates backwards from now, so the walk continues until the
exchange stops returning rows.

Usage:
    python3 backfill_funding.py                 # default symbol set
    python3 backfill_funding.py --symbols BTCUSDT ETHUSDT
    python3 backfill_funding.py --since-days 400
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
DB = BASE / "data" / "trading.db"
sys.path.insert(0, str(BASE / "scripts"))

import bybit_demo as bd  # noqa: E402

# The symbols this system actually trades or tests, in Bybit linear naming.
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
                   "BNBUSDT", "ADAUSDT", "LINKUSDT"]


def ensure_table(con: sqlite3.Connection) -> None:
    con.execute("""
        create table if not exists funding (
            symbol text not null,
            ts     integer not null,       -- ms epoch of the funding stamp
            rate   real not null,          -- fraction, 8h period
            primary key (symbol, ts)
        )""")
    con.commit()


def fetch_page(symbol: str, end_ms: int | None, limit: int = 200) -> list[dict]:
    params = {"category": "linear", "symbol": symbol, "limit": limit}
    if end_ms:
        params["endTime"] = end_ms
    r = bd.public("/v5/market/funding/history", params)
    if r.get("retCode") != 0:
        raise RuntimeError(f"{symbol}: {r.get('retMsg')}")
    return r.get("result", {}).get("list", []) or []


def backfill(con: sqlite3.Connection, symbol: str, since_ms: int) -> tuple[int, int]:
    """Insert missing funding rows for one symbol. Returns (fetched, inserted)."""
    have = {r[0] for r in con.execute(
        "select ts from funding where symbol=?", (symbol,))}
    fetched = inserted = 0
    end = None
    # Bybit returns newest-first and pages backwards; stop when we pass since_ms
    # or the exchange stops returning rows.
    while True:
        rows = fetch_page(symbol, end)
        if not rows:
            break
        oldest = None
        for x in rows:
            ts = int(x["fundingRateTimestamp"])
            rate = float(x["fundingRate"])
            fetched += 1
            oldest = ts if oldest is None else min(oldest, ts)
            if ts >= since_ms and ts not in have:
                con.execute("insert or ignore into funding(symbol,ts,rate) values (?,?,?)",
                            (symbol, ts, rate))
                inserted += 1
        con.commit()
        if oldest is None or oldest <= since_ms:
            break
        end = oldest - 1
        time.sleep(0.25)          # be polite to the public endpoint
    return fetched, inserted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    ap.add_argument("--since-days", type=int, default=400)
    args = ap.parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=args.since_days)
    since_ms = int(since.timestamp() * 1000)

    con = sqlite3.connect(DB)
    ensure_table(con)

    print("=" * 68)
    print(f"FUNDING BACKFILL — since {since:%Y-%m-%d} ({args.since_days}d)")
    print("=" * 68)

    total_new = 0
    for sym in args.symbols:
        try:
            fetched, inserted = backfill(con, sym, since_ms)
        except Exception as e:
            print(f"  {sym:9} FAILED: {str(e)[:90]}")
            continue
        total_new += inserted
        n = con.execute("select count(*), min(ts), max(ts) from funding where symbol=?",
                        (sym,)).fetchone()
        if n and n[0]:
            lo = datetime.fromtimestamp(n[1] / 1000, timezone.utc)
            hi = datetime.fromtimestamp(n[2] / 1000, timezone.utc)
            print(f"  {sym:9} {n[0]:5} rows  {lo:%Y-%m-%d} .. {hi:%Y-%m-%d}  (+{inserted} new)")
        else:
            print(f"  {sym:9} no rows stored")

    tot = con.execute("select count(*) from funding").fetchone()[0]
    print(f"\n  {total_new} new rows; {tot} total in funding table")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
