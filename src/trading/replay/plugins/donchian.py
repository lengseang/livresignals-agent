"""Donchian ensemble with volatility targeting (candidate A1). REPLAY/PAPER only.

Re-implementation of Zarattini, Pagani & Barbon (2025, SSRN 5209907) from the rules recorded in
research/candidates/candidates.json (A1) before any run (secondary source: CXO Advisory summary):
  lookbacks 5, 10, 20, 30, 60, 90, 150, 250, 360 days, per coin, daily;
  a lookback opens when the daily close is above the max close of the prior N days;
  it closes when the close falls below its trailing stop = max(prior stop, channel midpoint);
  coin signal = equal-weight share of open lookbacks (0..1);
  size by a 25% annualised volatility target using 90-day realised volatility;
  universe: listed >= 1 year and median 30-day daily dollar volume >= $2M, the 20 most liquid; daily rebalance.
Declared deviations: our 15-38 coins, not the full market; spot only, the vol scale is capped at 1 (can only
scale down); channel = max/min of CLOSES of the prior N days (midpoint = their mean); each eligible coin gets an
equal 1/K share of equity before signal and vol scaling (the summary does not state the per-coin budget).
"""
from __future__ import annotations

import math

import numpy as np

from trading.replay.bars import INTERVAL_MS, Bar
from trading.replay.strategy import Signal

D = INTERVAL_MS["1d"]
LOOKBACKS = (5, 10, 20, 30, 60, 90, 150, 250, 360)


def vol_scale(c: np.ndarray, vol_days: int, target: float) -> float:
    """target / annualised realised vol of the last `vol_days` daily log returns, capped at 1 (spot: scale down only)."""
    r = np.diff(np.log(c[-vol_days - 1:]))
    vol = float(r.std(ddof=1)) * math.sqrt(365) if len(r) > 1 else math.inf
    return min(1.0, target / vol) if vol > 0 else 0.0


class DonchianEnsemble:
    intervals = ("1d",)
    caveats = ("A1 rules from a secondary summary (SSRN blocked); per-coin 1/K budget, close-based channels and "
               "the spot cap on the vol scale are our declared choices. See research/candidates/candidates.json A1.",)

    def __init__(self, lookbacks: tuple = LOOKBACKS, vol_target: float = 0.25, vol_days: int = 90,
                 min_age_days: int = 365, min_median_volume: float = 2_000_000.0, top_n: int = 20,
                 btc: str = "BTCUSDT"):
        self.name = "donchian_ensemble"
        self.params = {"lookbacks": list(lookbacks), "vol_target": vol_target, "vol_days": vol_days,
                       "min_age_days": min_age_days, "min_median_volume": min_median_volume, "top_n": top_n,
                       "interval": "1d", "candidate": "A1-donchian-ensemble"}
        self._lb, self._vt, self._vd = tuple(lookbacks), vol_target, vol_days
        self._age, self._minvol, self._top, self._btc = min_age_days, min_median_volume, top_n, btc
        self._seen: list[str] = []
        self._stop: dict[tuple[str, int], float] = {}   # (symbol, lookback) -> trailing stop while open

    def _signal(self, sym: str, c: np.ndarray) -> float:
        """Share of lookbacks open after today's close (updates each lookback's state)."""
        opened = 0
        for n in self._lb:
            key = (sym, n)
            if len(c) < n + 1:
                self._stop.pop(key, None)
                continue
            prior = c[-n - 1:-1]
            hi, lo, today = prior.max(), prior.min(), c[-1]
            mid = (hi + lo) / 2
            if key in self._stop:
                stop = max(self._stop[key], mid)
                if today < stop:
                    del self._stop[key]
                else:
                    self._stop[key] = stop
                    opened += 1
            elif today > hi:
                self._stop[key] = mid
                opened += 1
        return opened / len(self._lb)

    def on_bar(self, bar: Bar, ctx) -> list[Signal]:
        if bar.symbol not in self._seen:
            self._seen.append(bar.symbol)
        if bar.symbol != self._btc:
            return []
        t, need = bar.end_ms, max(max(self._lb) + 1, self._age, self._vd + 1)
        eligible = []
        for sym in sorted(self._seen):
            h = ctx.history(sym, "1d", need)
            if not h or h[-1].end_ms != t:
                continue
            c = np.array([b.close for b in h])
            sig = self._signal(sym, c)                  # keep every lookback's state current, eligible or not
            if len(h) < self._age:
                continue
            medv = float(np.median([b.quote_volume for b in h[-30:]]))
            if medv < self._minvol:
                continue
            scale = vol_scale(c, self._vd, self._vt)
            eligible.append((medv, sym, sig * scale, c[-1]))
        eligible.sort(key=lambda x: (-x[0], x[1]))
        chosen = eligible[: self._top]
        k = len(chosen)
        target = {sym: (w / k if k else 0.0) for _, sym, w, _ in chosen}
        px = {sym: p for _, sym, _, p in chosen}
        now, out = ctx.now_ms, []
        for sym in sorted(self._seen):
            wt = target.get(sym, 0.0)
            if wt == 0.0 and ctx.position(sym) is None:
                continue
            out.append(Signal(self.name, sym, "target", now, f"ensemble x vol scale -> weight {wt:.4f}",
                              px.get(sym, 0.0), "1d", weight=wt))
        return out
