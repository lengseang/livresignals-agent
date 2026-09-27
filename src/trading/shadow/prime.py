"""Priming (P2 design §3, amended): bring every book's strategy state up to the present before the forward record.

1. history: monthly 1m klines, then daily 1m klines after the last month (`history --kinds klines_1m`), fed through
   each book's own LoopCore in ONE merged pass (the same step() replay runs, so priming == replay);
2. catch-up: the live recorder's ticks for the hours after the last daily file, fed through Scanner.on_tick;
3. then the live stream.
During 1-2 nothing is written to the chain (the sink is detached); the forward record starts after them.
Amendment to the design: the paper account is NOT reset after priming. Positions carry over, so a primed book is
exactly the replay of the same span, and the scorer measures returns from the first forward mark.
"""
from __future__ import annotations

import heapq
import re
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path

from trading.replay import engine, loader
from trading.replay.bars import Bar

_DAY = re.compile(r"^(?P<sym>[A-Z0-9]+)-1m-(?P<d>\d{4}-\d{2}-\d{2})\.zip$")


def history_bars(history_root: Path | str, symbol: str, start_ms: int, end_ms: int) -> Iterator[Bar]:
    """Monthly then daily 1m bars of `symbol` with start_ms <= open and end <= end_ms, strictly increasing."""
    root = Path(history_root)
    last = -1
    for b in loader.iter_symbol(root / "monthly", symbol, start_ms, end_ms):
        last = b.open_ms
        yield b
    days = sorted(p for p in (root / "klines_1m" / symbol).glob(f"{symbol}-1m-*.zip") if _DAY.match(p.name))
    for p in days:
        for b in loader.iter_klines(p, symbol):
            if b.open_ms <= last or b.open_ms < start_ms:
                continue
            if b.end_ms > end_ms:
                return
            last = b.open_ms
            yield b


def prime_history(fleet, bars_for: Callable[[str], Iterable[Bar]]) -> dict:
    """One merged pass over every symbol's bars, stepping every live book's LoopCore at each instant."""
    books = [b for b in fleet.books if not b.stopped]
    merged = heapq.merge(*(engine._events(bars_for(s)) for s in fleet.symbols), key=lambda e: (e[0], e[1], e[2]))
    steps, last_t = 0, None
    for t, group in groupby(merged, key=lambda e: e[0]):
        evs = list(group)
        closes = [e[3] for e in evs if e[1] == engine._CLOSE]
        opens = [e[3] for e in evs if e[1] == engine._OPEN]
        for b in books:
            try:
                b.scanner.core.step(t, closes, opens)
            except Exception as e:  # noqa: BLE001 - a plugin that cannot prime is stopped, not the fleet
                fleet._stop(b, e, t)
        books = [b for b in books if not b.stopped]
        steps, last_t = steps + 1, t
    for b in fleet.books:
        b.drained = len(b.account.events)          # priming fills are history, not forward records
    return {"steps": steps, "primed_through_ms": last_t}


def catch_up(fleet, ticks: Iterable) -> int:
    """Recorder ticks after the history: through the live code path, with every chain detached."""
    sinks = {id(b): b.scanner.sink for b in fleet.books}
    for b in fleet.books:
        b.scanner.sink = None
    n = 0
    try:
        for tick in ticks:
            for b in fleet.books:
                if not b.stopped:
                    try:
                        b.scanner.on_tick(tick)
                    except Exception as e:  # noqa: BLE001
                        fleet._stop(b, e, getattr(tick, "ts_exchange_ms", None))
            n += 1
    finally:
        for b in fleet.books:
            b.scanner.sink = sinks[id(b)]
            b.drained = len(b.account.events)
    return n


def utc_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)
