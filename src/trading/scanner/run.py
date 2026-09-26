"""Run the scanner (paper only). A mode must be chosen explicitly; there is no default.

Offline, through the exact live code path (ticks -> bars -> LoopCore), output labelled REPLAY:
    python -m trading.scanner.run --once-replay data/local/ticks/2026-09-26 --symbols BTCUSDT,ETHUSDT
    python -m trading.scanner.run --once-replay .../BTCUSDT-1m-2025-10.zip .../ETHUSDT-1m-2025-10.zip

  Inputs: recorder files (<day>/kline/<HH>.jsonl[.gz], Tick records), raw combined-stream lines, a
  directory of those, or monthly 1m kline zips, which are turned into SYNTHETIC 1s ticks (ticks_from_bar).
  Output: <out>/once-replay/<run_id>.jsonl. Offline runs never write the dated LIVE_PAPER files.

Live (opt-in, LIVE_PAPER): kline_1s from the market-data-only mirror (trading.stream.ws_client):
    python -m trading.scanner.run --live --minutes 2 --symbols BTCUSDT,ETHUSDT
  Output: <out>/<YYYY-MM-DD>.jsonl. Nothing here can place an order: no exchange client is imported.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import heapq
import json
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from trading.replay import loader, windows
from trading.scanner.bars import ticks_from_bar
from trading.scanner.registry import DEFAULT
from trading.scanner.scanner import Scanner
from trading.scanner.sink import DailySink, FileSink
from trading.stream.normalize import Tick, parse

LABEL = "SIMULATED (paper only; research, not evidence of edge)"


def _lines(path: Path) -> Iterator[str]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        yield from f


def _ticks_from_file(path: Path) -> Iterator[Tick]:
    for line in _lines(path):
        line = line.strip()
        if not line:
            continue
        if line.startswith('{"stream"'):
            t = parse(line, 0)
            if t is not None:
                yield t
            continue
        try:
            yield Tick(**json.loads(line))
        except (ValueError, TypeError):
            continue


def _zip_ticks(paths: list[Path], open_holdout: bool) -> Iterator[Tick]:
    """Monthly 1m kline zips -> synthetic 1s ticks, merged across symbols in exchange time order."""
    gens = []
    for p in paths:
        m = loader._MONTH.match(p.name)
        if not m or m["iv"] != "1m":
            raise SystemExit(f"{p.name}: expected <SYM>-1m-YYYY-MM.zip")
        last = loader.month_files(p.parent.parent, m["sym"])[-1][0]
        hold = windows.holdout_start(last)
        if m["ym"] >= hold and not open_holdout:
            raise SystemExit(f"{p.name} is in the holdout (from {hold}); pass --open-holdout only for a final gate")

        def gen(path=p, sym=m["sym"]):
            for bar in loader.iter_klines(path, sym):
                yield from ticks_from_bar(bar)
        gens.append(gen())
    return heapq.merge(*gens, key=lambda t: (t.data["start_ms"], t.symbol))


def _input_ticks(paths: list[Path], open_holdout: bool) -> Iterator[Tick]:
    zips = [p for p in paths if p.suffix == ".zip"]
    if zips:
        if len(zips) != len(paths):
            raise SystemExit("mix of zips and tick files; run them separately")
        yield from _zip_ticks(zips, open_holdout)
        return
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files += sorted(x for x in p.rglob("*") if x.is_file() and x.name.endswith((".jsonl", ".jsonl.gz"))
                            and "kline" in x.parts)
        else:
            files.append(p)
    for f in files:
        yield from _ticks_from_file(f)


def _strategies(names: str, params: list[str]):
    over: dict[str, dict] = {}
    for kv in params:
        key, _, val = kv.partition("=")
        name, _, field = key.partition(".")
        try:
            v = json.loads(val)
        except ValueError:
            v = val
        over.setdefault(name, {})[field] = v
    return [DEFAULT.build(n, **over.get(n, {})) for n in names.split(",") if n]


def _summary(sc: Scanner, mode: str, run_id: str, path, secs: float) -> dict:
    return {"mode": mode, "label": LABEL, "run_id": run_id, "symbols": sc.symbols,
            "strategies": [s.name for s in sc.core.strategies], "ticks": sc.stats["ticks"],
            "bars_closed": sc.stats["bars_closed"], "instants": sc.stats["instants"],
            "signals": sc.stats["signals"], "late_events": sc.stats["late_events"],
            "late_opens_applied": sc.stats["late_opens_applied"],
            "builder": dict(sc.builder.stats), "out": str(path) if path else None, "seconds": round(secs, 1)}


def once_replay(a) -> dict:
    run_id = "scanner-once-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sink = FileSink(Path(a.out) / "once-replay" / f"{run_id}.jsonl", run_id)
    sc = Scanner(a.symbols.split(","), _strategies(a.strategies, a.param), mode="REPLAY", run_id=run_id,
                 sink=sink, source="kline_1s", grace_ms=None)
    t0 = time.time()
    try:
        for t in _input_ticks([Path(p) for p in a.once_replay], a.open_holdout):
            sc.on_tick(t)
        sc.finish()
    finally:
        path = sink.path
        sink.close()
    return _summary(sc, "REPLAY", run_id, path, time.time() - t0)


async def _live(a) -> dict:
    from trading.stream.ws_client import build_url, messages   # the market-data-only mirror

    sink = DailySink(a.out)
    syms = a.symbols.split(",")
    sc = Scanner(syms, _strategies(a.strategies, a.param), mode="LIVE_PAPER", sink=sink,
                 source="kline_1s", grace_ms=a.grace_ms)
    url = build_url([s.lower() for s in syms], ["kline_1s"])
    stop_at = time.monotonic() + a.minutes * 60
    t0, reconnects, backoff, last_report = time.time(), 0, 1.0, time.monotonic()
    try:
        while time.monotonic() < stop_at:
            try:
                async for raw, ts in messages(url, stall_s=30.0):
                    backoff = 1.0
                    t = parse(raw, ts)
                    if t is not None:
                        for rec in sc.on_tick(t):
                            print(f"SIGNAL (simulated) {rec['data']['strategy_id']} {rec['data']['symbol']} "
                                  f"{rec['data']['action']} @ {rec['data']['ref_price']}", flush=True)
                    if time.monotonic() - last_report >= 30:
                        print(f"[{datetime.now(timezone.utc):%H:%M:%S}] ticks={sc.stats['ticks']} "
                              f"bars={sc.stats['bars_closed']} instants={sc.stats['instants']} "
                              f"signals={sc.stats['signals']} late={sc.stats['late_events']}", flush=True)
                        last_report = time.monotonic()
                    if time.monotonic() >= stop_at:
                        break
            except Exception as e:  # noqa: BLE001 - any socket failure means reconnect
                print(f"connection error: {type(e).__name__}: {str(e)[:120]}", flush=True)
            if time.monotonic() >= stop_at:
                break
            reconnects += 1
            print(f"reconnecting; dropped {sc.reconnected()} bars in progress (gap)", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
    finally:
        path = sink.path
        sink.close()
    s = _summary(sc, "LIVE_PAPER", "live-<date>", path or Path(a.out), time.time() - t0)
    s["reconnects"] = reconnects
    return s


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once-replay", nargs="+", metavar="PATH", help="offline run over recorded/derived ticks")
    mode.add_argument("--live", action="store_true", help="opt-in live paper scanner (market data only)")
    mode.add_argument("--list", action="store_true", help="list registered strategy plugins")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--strategies", default="trend_sma")
    ap.add_argument("--param", action="append", default=[], metavar="STRAT.KEY=VALUE")
    ap.add_argument("--out", default="data/local/signals")
    ap.add_argument("--minutes", type=float, default=2.0, help="live: stop after N minutes")
    ap.add_argument("--grace-ms", type=int, default=3000, help="live: release an instant a silent symbol holds")
    ap.add_argument("--open-holdout", action="store_true")
    a = ap.parse_args(argv)

    if a.list:
        out = {"plugins": [{"name": p.name, "family": p.family, "params": p.params, "version": p.version,
                            "gate_status": p.gate_status, "evidence": p.evidence} for p in DEFAULT.list()]}
    elif a.live:
        out = asyncio.run(_live(a))
    else:
        out = once_replay(a)
    print(json.dumps(out, indent=2))
    return out


if __name__ == "__main__":
    main()
