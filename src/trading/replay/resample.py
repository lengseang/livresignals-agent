"""1m bars -> higher intervals, UTC-aligned. A bucket is emitted only once its period has ended.

A 1m bar belongs to the period in which it CLOSES (end_ms - 1). Real late-2017 bars are phase-shifted
(~20 s), so a bar can straddle midnight; assigning it by close means every bar in a bucket is known by
the bucket's end, and the bucket never needs a late addition after it was emitted.
"""
from __future__ import annotations

from trading.replay.bars import INTERVAL_MS, Bar


class Resampler:
    __slots__ = ("symbol", "interval", "step", "_b")

    def __init__(self, symbol: str, interval: str):
        self.symbol, self.interval, self.step = symbol, interval, INTERVAL_MS[interval]
        self._b: list | None = None     # [start, o, h, l, c, v, qv, n, tbb]

    def add(self, bar: Bar) -> list[Bar]:
        """Add a 1m bar; returns the previous bucket if this bar starts a new one."""
        k = bar.end_ms - 1
        start = k - k % self.step
        out = []
        b = self._b
        if b is not None and start != b[0]:
            out.append(self._emit())
            b = None
        if b is None:
            self._b = [start, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.quote_volume,
                       bar.trades, bar.taker_buy_base]
        else:
            if bar.high > b[2]:
                b[2] = bar.high
            if bar.low < b[3]:
                b[3] = bar.low
            b[4] = bar.close
            b[5] += bar.volume
            b[6] += bar.quote_volume
            b[7] += bar.trades
            b[8] += bar.taker_buy_base
        return out

    def flush_until(self, now_ms: int) -> Bar | None:
        """Emit the open bucket if its period has ended by now_ms (it can never end early)."""
        if self._b is not None and self._b[0] + self.step <= now_ms:
            return self._emit()
        return None

    def _emit(self) -> Bar:
        s, o, h, l, c, v, qv, n, tbb = self._b
        self._b = None
        return Bar(self.symbol, self.interval, s, s + self.step, o, h, l, c, v, qv, n, tbb)
