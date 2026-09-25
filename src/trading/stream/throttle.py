"""Keep bookTicker updates that move a best price, plus one snapshot per interval.

Quantity-only updates are ~all of bookTicker's volume and add nothing a replay
needs; they are counted in `dropped` so the share is measured, not assumed.
"""
from __future__ import annotations

from trading.stream.normalize import Tick


class BookThrottle:
    def __init__(self, min_interval_ns: int = 10**9) -> None:
        self.min_interval_ns = min_interval_ns
        self._last_px: dict[str, tuple[float, float]] = {}
        self._last_kept_ns: dict[str, int] = {}
        self.kept = 0
        self.dropped = 0

    def keep(self, t: Tick) -> bool:
        px = (t.data["bid"], t.data["ask"])
        moved = self._last_px.get(t.symbol) != px
        last_kept = self._last_kept_ns.get(t.symbol)
        due = last_kept is None or t.ts_recv_ns - last_kept >= self.min_interval_ns
        self._last_px[t.symbol] = px
        if moved or due:
            self._last_kept_ns[t.symbol] = t.ts_recv_ns
            self.kept += 1
            return True
        self.dropped += 1
        return False
