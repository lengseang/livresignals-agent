"""Live ticks -> the same 1m Bar objects the replay engine consumes. A bar is emitted only once closed.

Sources (one per builder; mixing them would double-count volume):
- "kline_1s": closed 1s klines. Binance sends one every second even when nothing traded (volume 0,
  price carried from the last trade). Such zero-trade seconds move time forward but never touch
  open/high/low/close, because a Binance 1m kline's open is the minute's first TRADE. A minute with no
  trade at all becomes a flat bar at the carried price, which is Binance's own convention for 1m klines.
- "aggTrade": aggregate trades. quote_volume = sum(price x qty), taker_buy_base = qty where the buyer
  was the taker, trades = last_id - first_id + 1. A minute with no trade has no bar (history has a flat
  one), and a trade stream only learns a minute is over when a later trade arrives.

Events are (t_ms, "open" | "close", symbol, obj):
- "open"  at the bar's open_ms, obj = BarOpen(open = first trade price), emitted when that trade arrives
- "close" at the bar's end_ms,  obj = the complete Bar, emitted when the minute is known to be over:
  the 1s kline of second :59 arrived, a tick of a later minute arrived, or close_through() was called.

No partial-bar leakage: a Bar object is created only at close and is frozen; a tick for a minute that
is already closed is dropped and counted ("late"), never merged into a bar that was already emitted.
A minute counts only if the builder was listening from its first instant: the minute in progress when
the scanner starts (or reconnects) is dropped and counted ("incomplete"), never emitted as a bar.
Grid: live klines are minute-aligned, so end_ms = open_ms + 60 s (the replay loader uses the real
close_time; on 2025+ history the two agree, which the equivalence tests check).
"""
from __future__ import annotations

import math
from collections.abc import Iterator

from trading.replay.bars import Bar, BarOpen
from trading.stream.normalize import Tick

MIN = 60_000
SOURCES = ("kline_1s", "aggTrade")


class _Acc:
    __slots__ = ("open_ms", "complete", "traded", "open_sent", "o", "h", "l", "c", "v", "qv", "n", "tbb",
                 "carried")

    def __init__(self, open_ms: int, complete: bool):
        self.open_ms = open_ms
        self.complete = complete                # observed from its first instant (else never emitted)
        self.traded = False
        self.open_sent = not complete           # an incomplete minute never announces an OPEN
        self.o = self.h = self.l = self.c = 0.0
        self.v = self.qv = self.tbb = 0.0
        self.n = 0
        self.carried: float | None = None       # price of a zero-trade 1s kline (for a flat minute)


class BarBuilder:
    def __init__(self, source: str = "kline_1s"):
        if source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}, not {source!r}")
        self.source = source
        self._acc: dict[str, _Acc] = {}
        self._last_key: dict[str, int] = {}          # last 1s start_ms / agg id per symbol
        self._closed_until: dict[str, int] = {}      # end_ms of the last closed bar per symbol
        self._since: dict[str, int] = {}             # first tick time since start / last reconnect
        self.last_time: int | None = None            # exchange time of the last accepted tick
        self.stats = {"ticks": 0, "ignored": 0, "unclosed": 0, "duplicate": 0, "late": 0,
                      "bars": 0, "flat_bars": 0, "incomplete": 0,
                      "discarded_open": 0}

    # --- queries used by the scanner's release rule ----------------------------------------
    def pending_open_ms(self, symbol: str) -> int | None:
        """open_ms of a bar that has started but whose OPEN event has not been emitted yet."""
        a = self._acc.get(symbol)
        return a.open_ms if a is not None and not a.open_sent else None

    def open_bar_end(self, symbol: str) -> int | None:
        a = self._acc.get(symbol)
        return None if a is None else a.open_ms + MIN

    # --- feeding -------------------------------------------------------------------------------
    def feed(self, t: Tick) -> list[tuple]:
        self.last_time = None
        if self.source == "kline_1s":
            if t.kind != "kline" or t.data.get("interval") != "1s":
                self.stats["ignored"] += 1
                return []
            if not t.data["closed"]:
                self.stats["unclosed"] += 1
                return []
            ts = key = t.data["start_ms"]
        else:
            if t.kind != "trade":
                self.stats["ignored"] += 1
                return []
            ts, key = t.data["trade_ms"], t.data["agg_id"]
        sym = t.symbol
        last = self._last_key.get(sym)
        if last is not None and key <= last:
            self.stats["duplicate" if key == last else "late"] += 1
            return []
        m = ts - ts % MIN
        acc = self._acc.get(sym)
        if m + MIN <= self._closed_until.get(sym, -1) or (acc is not None and m < acc.open_ms):
            self.stats["late"] += 1
            return []
        self._last_key[sym] = key
        since = self._since.setdefault(sym, ts)
        self.stats["ticks"] += 1
        self.last_time = ts
        out: list[tuple] = []
        if acc is not None and m != acc.open_ms:
            out.extend(self._close(sym))
            acc = None
        if acc is None:
            acc = self._acc[sym] = _Acc(m, complete=m >= since)
        if self.source == "kline_1s":
            d = t.data
            if d["trades"] > 0:
                self._trade(acc, d["open"], d["high"], d["low"], d["close"], d["volume"],
                            _num(d.get("quote_volume")), d["trades"], _num(d.get("taker_buy_base")))
            elif acc.carried is None:
                acc.carried = d["close"]
            if not acc.open_sent and acc.traded:
                acc.open_sent = True
                out.append((m, "open", sym, BarOpen(sym, "1m", m, acc.o)))
            if ts - m == MIN - 1000:                     # second :59 closed: the minute is over
                out.extend(self._close(sym))
        else:
            d = t.data
            p, q = d["price"], d["qty"]
            self._trade(acc, p, p, p, p, q, p * q, d["last_id"] - d["first_id"] + 1,
                        0.0 if d["buyer_is_maker"] else q)
            if not acc.open_sent:
                acc.open_sent = True
                out.append((m, "open", sym, BarOpen(sym, "1m", m, acc.o)))
        return out

    @staticmethod
    def _trade(acc: _Acc, o, h, l, c, v, qv, n, tbb) -> None:
        if not acc.traded:
            acc.traded = True
            acc.o, acc.h, acc.l = o, h, l
        else:
            if h > acc.h:
                acc.h = h
            if l < acc.l:
                acc.l = l
        acc.c = c
        acc.v += v
        acc.qv += qv
        acc.n += n
        acc.tbb += tbb

    def close_through(self, symbol: str, now_ms: int) -> list[tuple]:
        """Close symbol's open bar if its minute ended by now_ms (a stalled stream; used with a grace)."""
        a = self._acc.get(symbol)
        if a is None or a.open_ms + MIN > now_ms:
            return []
        return self._close(symbol)

    def discard_open(self) -> int:
        """Drop every bar in progress (after a reconnect the minute has a hole). Returns how many.

        The next minute of each symbol counts only if it is observed from its first instant again.
        """
        n = len(self._acc)
        for sym, a in self._acc.items():
            self._closed_until[sym] = max(self._closed_until.get(sym, -1), a.open_ms + MIN)
        self._acc.clear()
        self._since.clear()
        self.stats["discarded_open"] += n
        return n

    def _close(self, sym: str) -> list[tuple]:
        a = self._acc.pop(sym)
        end = a.open_ms + MIN
        self._closed_until[sym] = end
        out = []
        if not a.complete:                               # started listening mid-minute: a partial bar
            self.stats["incomplete"] += 1
            return []
        if a.traded:
            bar = Bar(sym, "1m", a.open_ms, end, a.o, a.h, a.l, a.c, a.v, a.qv, a.n, a.tbb)
        elif a.carried is not None:                      # a minute with no trade: flat at the carried price
            p = a.carried
            bar = Bar(sym, "1m", a.open_ms, end, p, p, p, p, 0.0, 0.0, 0, 0.0)
            self.stats["flat_bars"] += 1
            a.open_sent = True
            out.append((a.open_ms, "open", sym, BarOpen(sym, "1m", a.open_ms, p)))
        else:
            return []
        self.stats["bars"] += 1
        out.append((end, "close", sym, bar))
        return out


def _num(x) -> float:
    """Missing quote_volume / taker_buy_base (ticks recorded before T3.2) become NaN: unknown, not 0."""
    return math.nan if x is None else x


def ticks_from_bar(b: Bar, recv_ns: int = 0) -> Iterator[Tick]:
    """Closed 1s-kline ticks that rebuild exactly `b` (a test/offline helper; SYNTHETIC ticks).

    Each distinct price the bar needs (open, high, low, close) is one traded second; all volume sits in
    the first so sums are exact; a zero-trade second :59 closes the minute, as the live stream does.
    A zero-trade bar becomes zero-trade seconds only (a flat minute).
    """
    if b.end_ms - b.open_ms != MIN or b.open_ms % MIN:
        raise ValueError(f"{b.symbol} bar at {b.open_ms} is not a grid-aligned 1m bar")

    def k(sec: int, price: float, v=0.0, qv=0.0, n=0, tbb=0.0) -> Tick:
        start = b.open_ms + sec * 1000
        return Tick("kline", b.symbol, start + 1000, recv_ns, {
            "interval": "1s", "start_ms": start, "open": price, "high": price, "low": price, "close": price,
            "volume": v, "trades": n, "closed": True, "quote_volume": qv, "taker_buy_base": tbb})

    if b.trades == 0:
        if not b.open == b.high == b.low == b.close:
            raise ValueError(f"zero-trade bar at {b.open_ms} is not flat")
        yield k(0, b.open)
        yield k(59, b.open)
        return
    pts = [b.open]
    if b.high != max(b.open, b.close):
        pts.append(b.high)
    if b.low != min(b.open, b.close):
        pts.append(b.low)
    if pts[-1] != b.close:
        pts.append(b.close)
    if len(pts) > b.trades:
        raise ValueError(f"bar at {b.open_ms} has {b.trades} trades but needs {len(pts)} distinct prints")
    secs = list(range(len(pts) - 1)) + [58] if len(pts) > 1 else [0]
    for i, (sec, p) in enumerate(zip(secs, pts)):
        if i == 0:
            yield k(sec, p, b.volume, b.quote_volume, b.trades - (len(pts) - 1), b.taker_buy_base)
        else:
            yield k(sec, p, n=1)
    yield k(59, b.close)
