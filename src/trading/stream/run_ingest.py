"""Record per-second ticks to disk: python -m trading.stream.run_ingest [--hours N]

Paper-only market data. Every reconnect and every aggTrade id jump is written as
a gap record; nothing is interpolated.
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trading.stream.book import TopOfBook
from trading.stream.clock import Due, measure
from trading.stream.gaps import AggGapDetector
from trading.stream.normalize import parse
from trading.stream.recorder import Recorder
from trading.stream.throttle import BookThrottle
from trading.stream.ws_client import build_url, messages

# Stream names need the full pair ("btcusdt"); an unknown stream name is accepted
# by the server and then simply never sends anything.
REAL_SYMS = [f"{b}usdt" for b in ("btc", "eth", "sol", "xrp", "bnb", "ada", "link", "doge", "avax",
                                   "near", "sui", "arb", "dot", "ltc", "atom")]
KINDS = ["bookTicker", "aggTrade", "kline_1s"]


def prune(root: Path, keep_days: int) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    for day in root.glob("????-??-??"):
        if day.is_dir() and day.name < cutoff:
            shutil.rmtree(day)


async def run(symbols: list[str], root: Path, hours: float | None, keep_days: int) -> dict:
    rec, gaps, throttle, tob = Recorder(root), AggGapDetector(), BookThrottle(), TopOfBook()
    url = build_url(symbols, KINDS)
    stats = {"messages": 0, "reconnects": 0, "agg_gaps": 0}
    stop_at = time.monotonic() + hours * 3600 if hours else None
    backoff, last_report = 1.0, time.monotonic()
    clock_due = Due(3600.0)
    prune(root, keep_days)
    try:
        while stop_at is None or time.monotonic() < stop_at:
            try:
                async for raw, ts in messages(url):
                    backoff = 1.0
                    t = parse(raw, ts)
                    if t is None:
                        continue
                    stats["messages"] += 1
                    tob.update(t)
                    if t.kind == "trade":
                        gap = gaps.feed(t.symbol, t.data["agg_id"])
                        if gap:
                            stats["agg_gaps"] += 1
                            rec.write("gap", {**gap, "ts_recv_ns": ts}, ts)
                    if t.kind == "book" and not throttle.keep(t):
                        continue
                    rec.write(t.kind, t.to_record(), ts)
                    if clock_due.due(time.monotonic()):
                        # blocking but short (10 s timeout, hourly); the socket buffers meanwhile
                        c = measure()
                        rec.write("clock", c, c["ts_recv_ns"])
                    if time.monotonic() - last_report >= 60:
                        rec.flush()
                        prune(root, keep_days)
                        print(f"[{datetime.now(timezone.utc):%H:%M:%S}] msgs={stats['messages']} "
                              f"book kept={throttle.kept} dropped={throttle.dropped} "
                              f"gaps={stats['agg_gaps']} reconnects={stats['reconnects']}", flush=True)
                        last_report = time.monotonic()
                    if stop_at is not None and time.monotonic() >= stop_at:
                        break
            except Exception as e:  # noqa: BLE001 - any socket failure means reconnect
                print(f"connection error: {type(e).__name__}: {str(e)[:120]}", flush=True)
            if stop_at is not None and time.monotonic() >= stop_at:
                break
            print(f"connection ended after {stats['messages']} messages total; reconnecting "
                  f"(a stall with 0 messages usually means a bad stream name)", flush=True)
            now = time.time_ns()
            stats["reconnects"] += 1
            gaps.reset()
            rec.write("gap", {"kind": "reconnect", "ts_recv_ns": now}, now)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
    finally:
        rec.close()
    stats.update(book_kept=throttle.kept, book_dropped=throttle.dropped)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=",".join(REAL_SYMS))
    ap.add_argument("--out", default="data/local/ticks")
    ap.add_argument("--hours", type=float, default=None, help="stop after N hours (default: run forever)")
    ap.add_argument("--keep-days", type=int, default=14)
    a = ap.parse_args()
    stats = asyncio.run(run(a.symbols.split(","), Path(a.out), a.hours, a.keep_days))
    print("done:", stats, flush=True)


if __name__ == "__main__":
    main()
