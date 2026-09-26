"""Range breakout with volume confirmation, 1h-4h (strategy-audit #8). REPLAY/PAPER only.

Rule (docs/loop/designs/T3.4.md, pre-registered): on each close of the strategy's interval for coin X,
  the range = the previous `n` bars (the current bar is NOT part of it);
  enter long if close > max(high of the range) and volume > vol_mult * mean(volume of the range).
Exits are the broker's: stop-loss `sl`, take-profit `tp`, time stop `max_hold_h`. A missing bar inside the
window means no signal. Long-only spot. Nothing here is evidence of edge.
"""
from __future__ import annotations

from trading.replay.bars import INTERVAL_MS, Bar
from trading.replay.strategy import Signal


class RangeBreakout:
    caveats = ("Long-only spot; entries are market orders at the next 1m open after the bar close.",)

    def __init__(self, interval: str = "1h", n: int = 24, vol_mult: float = 2.0, sl: float = 0.01,
                 tp: float = 0.02, max_hold_h: int = 24):
        self.name = "range_breakout"
        self.intervals = (interval,)
        self.params = {"interval": interval, "n": n, "vol_mult": vol_mult, "sl": sl, "tp": tp,
                       "max_hold_h": max_hold_h}
        self._iv, self._step, self._n, self._k = interval, INTERVAL_MS[interval], n, vol_mult
        self._sl, self._tp, self._hold = sl, tp, max_hold_h * INTERVAL_MS["1h"]

    def on_bar(self, bar: Bar, ctx) -> list[Signal]:
        if ctx.position(bar.symbol) is not None:
            return []
        h = ctx.history(bar.symbol, self._iv, self._n + 1)
        if len(h) < self._n + 1 or h[-1].end_ms - h[0].end_ms != self._n * self._step:
            return []                                               # warmup, or a missing bar
        prior, cur = h[:-1], h[-1]
        top = max(b.high for b in prior)
        avg_v = sum(b.volume for b in prior) / self._n
        if cur.close > top and cur.volume > self._k * avg_v:
            return [Signal(self.name, bar.symbol, "open", ctx.now_ms,
                           f"close>{self._n}x{self._iv} high & vol>{self._k:g}x avg", cur.close, self._iv,
                           sl=self._sl, tp=self._tp, max_hold_ms=self._hold)]
        return []
