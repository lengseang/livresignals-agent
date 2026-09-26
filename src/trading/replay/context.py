"""The clock, the bar store, and Context: the only window a strategy has on the world.

No-look-ahead is enforced here, on every read, not by convention: any bar a strategy could
receive with end_ms > now raises LookAheadError. Checks use `raise`, not `assert`, so
`python -O` cannot strip them.
"""
from __future__ import annotations

from collections import deque
from itertools import islice

from trading.replay.bars import Bar


class LookAheadError(RuntimeError):
    """A strategy or fill tried to use data from after the current clock time."""


class ClockError(LookAheadError):
    """The replay clock was asked to move backwards."""


class ReplayClock:
    __slots__ = ("now_ms",)

    def __init__(self, start_ms: int = 0):
        self.now_ms = start_ms

    def advance(self, t_ms: int) -> None:
        if t_ms < self.now_ms:
            raise ClockError(f"clock cannot move back from {self.now_ms} to {t_ms}")
        self.now_ms = t_ms


class BarStore:
    """Append-only closed bars per (symbol, interval). 1m history is capped (memory); others are kept."""

    def __init__(self, keep_1m: int = 1440):
        self._d: dict[tuple[str, str], deque[Bar]] = {}
        self._keep_1m = keep_1m

    def append(self, bar: Bar) -> None:
        key = (bar.symbol, bar.interval)
        q = self._d.get(key)
        if q is None:
            q = self._d[key] = deque(maxlen=self._keep_1m if bar.interval == "1m" else None)
        if q and bar.end_ms <= q[-1].end_ms:
            raise ValueError(f"{key}: bar ending {bar.end_ms} is not after {q[-1].end_ms}")
        q.append(bar)

    def tail(self, symbol: str, interval: str, n: int) -> tuple[Bar, ...]:
        q = self._d.get((symbol, interval))
        if not q or n <= 0:
            return ()
        return tuple(islice(q, max(0, len(q) - n), None))

    def all(self, symbol: str, interval: str) -> tuple[Bar, ...]:
        return tuple(self._d.get((symbol, interval), ()))


class Context:
    __slots__ = ("_store", "_clock", "_account")

    def __init__(self, store: BarStore, clock: ReplayClock, account):
        self._store, self._clock, self._account = store, clock, account

    @property
    def now_ms(self) -> int:
        return self._clock.now_ms

    def _checked(self, bars: tuple[Bar, ...]) -> tuple[Bar, ...]:
        # the store is append-only and monotonic, so the newest bar bounds all of them
        if bars and bars[-1].end_ms > self._clock.now_ms:
            raise LookAheadError(f"{bars[-1].symbol} {bars[-1].interval} bar ends {bars[-1].end_ms} "
                                 f"after now {self._clock.now_ms}")
        return bars

    def history(self, symbol: str, interval: str, n: int) -> tuple[Bar, ...]:
        """The last n closed bars (end_ms <= now), oldest first."""
        return self._checked(self._store.tail(symbol, interval, n))

    def last(self, symbol: str, interval: str) -> Bar | None:
        h = self.history(symbol, interval, 1)
        return h[0] if h else None

    def bar_at(self, symbol: str, interval: str, end_ms: int) -> Bar | None:
        if end_ms > self._clock.now_ms:
            raise LookAheadError(f"asked for {symbol} {interval} ending {end_ms}, now is {self._clock.now_ms}")
        for b in reversed(self._store.all(symbol, interval)):
            if b.end_ms == end_ms:
                return self._checked((b,))[0]
            if b.end_ms < end_ms:
                break
        return None

    def position(self, symbol: str):
        """(qty, entry_price) of the open paper position, or None. Read-only."""
        p = self._account.positions.get(symbol)
        return None if p is None else (p["qty"], p["entry"])
