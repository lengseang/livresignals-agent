"""Intraday momentum and reversal at an hourly horizon (strategy-audit #2). REPLAY/PAPER only.

Rule (docs/loop/designs/T3.4.md, pre-registered): on each 1h close of coin X,
  r_L = close / close[L bars ago] - 1;  sigma = sample std of the last `vol_h` hourly simple returns;
  z = r_L / (sigma * sqrt(L)).
  momentum: enter long if z >= +z_thr;  reversal: enter long if z <= -z_thr.
Exits are the broker's: stop-loss `sl`, take-profit `tp`, time stop `max_hold_h` (carried on the Signal).
A missing hour inside the bars used means no signal (no interpolation). Long-only spot.

Hypothesis source: Wen, Bouri, Xu, Zhao, N. Am. J. Econ. & Finance 2022 (intraday, 2013-2020). This is the hourly
version the audit asked for, not a replication, and nothing here is evidence of edge.
"""
from __future__ import annotations

import math

from trading.replay.bars import INTERVAL_MS, Bar
from trading.replay.strategy import Signal

H = INTERVAL_MS["1h"]


class _HourlyZ:
    intervals = ("1h",)
    direction = 0          # +1 momentum, -1 reversal
    name = ""
    caveats = ("Long-only spot; entries are market orders at the next 1m open after the 1h close.",
               "Hourly-horizon adaptation of an intraday paper (2013-2020 sample); not a replication.")

    def __init__(self, lookback_h: int = 4, z: float = 2.0, vol_h: int = 168, sl: float = 0.01,
                 tp: float = 0.02, max_hold_h: int = 24):
        self.params = {"lookback_h": lookback_h, "z": z, "vol_h": vol_h, "sl": sl, "tp": tp,
                       "max_hold_h": max_hold_h, "interval": "1h"}
        self._L, self._z, self._n = lookback_h, z, vol_h
        self._sl, self._tp, self._hold = sl, tp, max_hold_h * H
        self._need = max(vol_h, lookback_h) + 1

    def zscore(self, bars: tuple[Bar, ...]) -> float | None:
        if len(bars) < self._need or bars[-1].end_ms - bars[0].end_ms != (len(bars) - 1) * H:
            return None                                             # warmup, or a missing hour
        c = [b.close for b in bars]
        r = [c[i] / c[i - 1] - 1 for i in range(len(c) - self._n, len(c))]
        m = sum(r) / len(r)
        sd = math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))
        if sd == 0:
            return None
        return (c[-1] / c[-1 - self._L] - 1) / (sd * math.sqrt(self._L))

    def on_bar(self, bar: Bar, ctx) -> list[Signal]:
        if ctx.position(bar.symbol) is not None:
            return []
        z = self.zscore(ctx.history(bar.symbol, "1h", self._need))
        if z is None or z * self.direction < self._z:
            return []
        return [Signal(self.name, bar.symbol, "open", ctx.now_ms, f"z{self._L}h={z:+.2f}", bar.close, "1h",
                       sl=self._sl, tp=self._tp, max_hold_ms=self._hold)]


class IntradayMomentum(_HourlyZ):
    direction = 1

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.name = "intraday_momentum"


class IntradayReversal(_HourlyZ):
    direction = -1

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.name = "intraday_reversal"
