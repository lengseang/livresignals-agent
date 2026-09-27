"""Run the shadow scanner (paper only; P2 design). Every candidate card at stage validated/shadow/promoted runs in
the `candidate` lane, every rejected one in the `rejected` lane, plus the two benchmarks, each as an isolated paper
book with a hash-chained forward record.

    python -m trading.shadow.run --prime-only                     # prime from history, report, exit
    python -m trading.shadow.run --minutes 10                     # prime, catch up, then 10 live minutes
    python -m trading.shadow.run                                  # prime, catch up, run live until stopped

Order: history (monthly + daily 1m, `history --kinds klines_1m`) -> recorder ticks after it -> the live
market-data mirror. Nothing here can place an order: no exchange client is imported.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from trading.scanner.registry import DEFAULT
from trading.scanner.run import _ticks_from_file
from trading.shadow import prime
from trading.shadow.fleet import ShadowFleet
from trading.shadow.score import score

SYMBOLS = ("BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT,ADAUSDT,LINKUSDT,DOGEUSDT,AVAXUSDT,NEARUSDT,SUIUSDT,ARBUSDT,"
           "DOTUSDT,LTCUSDT,ATOMUSDT")
LANE_OF_STAGE = {"validated": "candidate", "shadow": "candidate", "promoted": "candidate", "rejected": "rejected"}
SLOTS = {"ctrend_lite": 3, "xs_momentum": 3}                 # top quintile of 15, as in the lab specs
DAY = 86_400_000


def plan(cards_path: Path | str, symbols: list[str], registry=DEFAULT) -> list[dict]:
    doc = json.loads(Path(cards_path).read_text(encoding="utf-8"))
    out = []
    for c in doc.get("candidates", []):
        lane = LANE_OF_STAGE.get(c.get("stage"))
        if lane is None or c.get("family") == "benchmark":
            continue
        try:
            registry.get(c["strategy"])
        except KeyError:
            continue                                         # a card without a plugin cannot run yet
        out.append({"lane": lane, "strategy": c["strategy"], "slots": SLOTS.get(c["strategy"]), "params": {}})
    out += [{"lane": "benchmark", "strategy": "buy_hold_btc", "slots": 1, "params": {}},
            {"lane": "benchmark", "strategy": "buy_hold_ew", "slots": len(symbols),
             "params": {"universe": tuple(symbols)}}]
    return out


def recorder_ticks(ticks_root: Path, after_ms: int):
    """kline ticks recorded after `after_ms`, day by day, hour by hour."""
    for day in sorted(p for p in Path(ticks_root).iterdir() if p.is_dir()):
        kdir = day / "kline"
        if not kdir.is_dir():
            continue
        for f in sorted(kdir.iterdir()):
            for t in _ticks_from_file(f):
                if t.ts_exchange_ms is not None and t.ts_exchange_ms > after_ms:
                    yield t


def build(a, now_ms: int) -> tuple[ShadowFleet, dict]:
    syms = a.symbols.split(",")
    day0 = now_ms - now_ms % DAY
    fleet = ShadowFleet(syms, plan(a.cards, syms), a.root, forward_start_ms=now_ms)
    start = day0 - a.prime_days * DAY
    t0 = time.time()
    rep = prime.prime_history(fleet, lambda s: prime.history_bars(a.history, s, start, day0))
    rep["prime_seconds"] = round(time.time() - t0, 1)
    rep["caught_up_ticks"] = prime.catch_up(fleet, recorder_ticks(Path(a.ticks), rep["primed_through_ms"] or start))
    for b in fleet.books:                     # a restarted process: the downtime is a gap, never back-filled
        if b.chain.resumed_from_ms is not None and b.chain.resumed_from_ms < now_ms:
            b.chain.write({"v": 1, "type": "gap", "ts_event_ms": now_ms, "mode": "LIVE_PAPER", "simulated": True,
                           "data": {"from_ms": b.chain.resumed_from_ms, "to_ms": now_ms, "reason": "process restart"}})
    rep["books"] = [{"lane": b.lane, "strategy": b.name, "stopped": b.stopped,
                     "equity_at_forward_start": round(b.account.equity(), 2),
                     "positions": sorted(b.account.positions)} for b in fleet.books]
    return fleet, rep


async def live(fleet: ShadowFleet, minutes: float | None, root: Path) -> dict:
    from trading.stream.normalize import parse
    from trading.stream.ws_client import build_url, messages    # the market-data-only mirror
    url = build_url([s.lower() for s in fleet.symbols], ["kline_1s"])
    stop_at = None if not minutes else time.monotonic() + minutes * 60
    reconnects, backoff, last_t, last_score = 0, 1.0, None, time.monotonic()
    while stop_at is None or time.monotonic() < stop_at:
        try:
            async for raw, ts in messages(url, stall_s=30.0):
                backoff = 1.0
                t = parse(raw, ts)
                if t is not None:
                    fleet.on_tick(t)
                    last_t = t.ts_exchange_ms or last_t
                if time.monotonic() - last_score >= 3600:
                    score(root)
                    last_score = time.monotonic()
                if stop_at is not None and time.monotonic() >= stop_at:
                    break
        except Exception as e:  # noqa: BLE001 - any socket failure means a gap and a reconnect
            print(f"connection error: {type(e).__name__}: {str(e)[:120]}", flush=True)
        if stop_at is not None and time.monotonic() >= stop_at:
            break
        reconnects += 1
        now = int(time.time() * 1000)
        fleet.gap(last_t or now, now, "stream reconnect")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60.0)
    return {"reconnects": reconnects}


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="data/local/shadow")
    ap.add_argument("--history", default="data/local/history")
    ap.add_argument("--ticks", default="data/local/ticks")
    ap.add_argument("--cards", default="research/candidates/candidates.json")
    ap.add_argument("--symbols", default=SYMBOLS)
    ap.add_argument("--prime-days", type=int, default=1100, help="history fed before going live (A2 needs ~900)")
    ap.add_argument("--minutes", type=float, default=None, help="live minutes; default: until stopped")
    ap.add_argument("--prime-only", action="store_true")
    a = ap.parse_args(argv)
    now = int(time.time() * 1000)
    fleet, rep = build(a, now)
    print(json.dumps({k: v for k, v in rep.items() if k != "books"}), flush=True)
    for b in rep["books"]:
        print(f"  {b['lane']:9} {b['strategy']:20} equity {b['equity_at_forward_start']:>10} "
              f"{'STOPPED ' + b['stopped'] if b['stopped'] else ''}", flush=True)
    if not a.prime_only:
        rep.update(asyncio.run(live(fleet, a.minutes, Path(a.root))))
        fleet.finish()
        score(a.root)
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] shadow run ended", flush=True)
    return rep


if __name__ == "__main__":
    main()
