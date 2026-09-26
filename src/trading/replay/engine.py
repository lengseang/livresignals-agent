"""The replay loop: 1m bars as time-ordered events across symbols, one clock, strategies see only the past.

Each 1m bar is two events: its OPEN at open_ms and its CLOSE at end_ms (Binance close_time + 1).
Real history is not always minute-aligned (late 2017 bars are phase-shifted by ~20 s), so the clock
follows the actual timestamps rather than assuming a grid. At each instant t:

  1. closes   bars ending at t enter the store, feed the resamplers, and mark the accounts
  2. emit     resampled bars whose period ended by t are stored (all symbols) BEFORE any strategy runs
  3. decide   strategies' on_bar(bar, ctx) for each emitted bar they subscribe to; signals queue
  4. record   equity of every account, marked to mid, while inside the trade window
  5. opens    bars opening at t fill queued orders at their OPEN (the first price after the signal)

Live (T3.2) runs this same loop body on closed live klines and the wall clock.
"""
from __future__ import annotations

import heapq
from array import array
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from itertools import groupby

from trading.replay.bars import INTERVAL_MS, Bar
from trading.replay.broker import PaperAccount
from trading.replay.context import BarStore, Context, ReplayClock
from trading.replay.resample import Resampler
from trading.replay.strategy import Signal

_CLOSE, _OPEN = 0, 1        # at the same instant, closes (and decisions) come before opens (fills)


@dataclass
class ReplayResult:
    store: BarStore
    ts: array = field(default_factory=lambda: array("q"))    # event times while trading
    signals: int = 0                                          # strategy signals inside the trade window
    warmup_signals: int = 0                                   # emitted before trade_start: not traded
    fills: dict = field(default_factory=dict)
    signal_events: list = field(default_factory=list)
    first_open: dict = field(default_factory=dict)            # symbol -> first 1m bar in the trade window
    last_close: dict = field(default_factory=dict)            # symbol -> last 1m bar closed
    steps: int = 0


def _events(stream: Iterable[Bar]) -> Iterator[tuple[int, int, str, Bar]]:
    prev_end = None
    for b in stream:
        if prev_end is not None and b.open_ms < prev_end:
            raise ValueError(f"{b.symbol}: bar opening {b.open_ms} overlaps the previous bar ending {prev_end}")
        yield (b.open_ms, _OPEN, b.symbol, b)
        yield (b.end_ms, _CLOSE, b.symbol, b)
        prev_end = b.end_ms


def replay(streams: Mapping[str, Iterable[Bar]], strategies: list, accounts: Mapping[str, PaperAccount],
           trade_start_ms: int | None = None, end_ms: int | None = None, keep_1m: int = 1440,
           run_id: str | None = None,
           benchmarks: Mapping[str, tuple[str, PaperAccount]] | None = None) -> ReplayResult:
    """Replay 1m bar streams (one per symbol). The first account is the one strategies' ctx.position reads.

    Every account receives the same signals; cost scenarios (gross/net/stress) run in one pass.
    `benchmarks` = {name: (symbol, account)}: buy-and-hold, bought at the symbol's first 1m open in
    the trade window through the same fill/cost code, and marked like the strategy.
    """
    store, clock = BarStore(keep_1m=keep_1m), ReplayClock()
    primary = next(iter(accounts.values()))
    ctx = Context(store, clock, primary)
    res = ReplayResult(store)
    want = {iv for s in strategies for iv in s.intervals}
    higher = sorted(iv for iv in want if iv != "1m")
    resamplers = {sym: [Resampler(sym, iv) for iv in higher] for sym in streams}
    all_rs = [r for rs in resamplers.values() for r in rs]
    by_interval: dict[str, list] = {}
    for s in strategies:
        for iv in s.intervals:
            by_interval.setdefault(iv, []).append(s)
    accts = list(accounts.values())
    bench = list((benchmarks or {}).values())
    marked = accts + [a for _, a in bench]
    start = trade_start_ms if trade_start_ms is not None else -(2**62)
    stop = end_ms if end_ms is not None else 2**62

    merged = heapq.merge(*(_events(st) for st in streams.values()), key=lambda e: (e[0], e[1], e[2]))
    for t, group in groupby(merged, key=lambda e: e[0]):
        if t > stop:
            break
        clock.advance(t)
        evs = list(group)
        trading = t >= start

        # 1. closes
        emitted: list[Bar] = []
        closed = [e[3] for e in evs if e[1] == _CLOSE]
        for b in closed:
            store.append(b)
            if "1m" in want:
                emitted.append(b)
            for r in resamplers[b.symbol]:
                emitted.extend(r.add(b))
            res.last_close[b.symbol] = b
            for a in marked:
                a.mark(b)
        # 2. emit
        if closed:
            for r in all_rs:
                e = r.flush_until(t)
                if e is not None:
                    emitted.append(e)
        emitted.sort(key=lambda x: (x.end_ms, INTERVAL_MS[x.interval], x.symbol))
        for e in emitted:
            if e.interval != "1m":
                store.append(e)
        # 3. decide
        for e in emitted:
            for s in by_interval.get(e.interval, ()):
                for sig in s.on_bar(e, ctx) or ():
                    if sig.ts_ms != t:
                        raise ValueError(f"{s.name}: signal ts {sig.ts_ms} is not the clock time {t}")
                    if trading:
                        res.signals += 1
                        res.signal_events.append(sig.record("REPLAY", run_id, getattr(s, "params", {})))
                        for a in accts:
                            a.submit(sig)
                    else:
                        res.warmup_signals += 1
        # 4. record, before this instant's fills: a fill's cost shows in the next mark, so a window
        #    that starts at t includes the entries made at t
        if trading and closed:
            res.ts.append(t)
            for a in marked:
                a.record()
        # 5. opens
        if trading and t < stop:
            for e in evs:
                if e[1] != _OPEN:
                    continue
                b = e[3]
                if b.symbol not in res.first_open:
                    res.first_open[b.symbol] = b
                    for sym, a in bench:
                        if sym == b.symbol:
                            a.submit(Signal("buy_and_hold", sym, "open", t, "benchmark", b.open, "1m"))
                for a in marked:
                    a.on_open(b)
        res.steps += 1

    res.fills = {name: a.fills for name, a in accounts.items()}
    return res
