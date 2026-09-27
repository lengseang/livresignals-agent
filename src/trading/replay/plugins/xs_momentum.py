"""Cross-sectional momentum (candidate A6). REPLAY/PAPER only.

Re-implementation of the three-week momentum strategy in Liu, Tsyvinski & Wu (2022), "Common Risk Factors in
Cryptocurrency", Journal of Finance 77(2) (NBER w25882, section on momentum, Table 4), rules recorded in
research/candidates/candidates.json (A6) before any run:
  each week, sort coins into quintiles on their past three-week return; long the top quintile
  (the paper's long-short top-minus-bottom earns 4.1%/week for three-week momentum, value-weighted).
Declared deviations: 15 Binance coins, not all coins above $1M market cap; long-only top quintile; equal weights
(no market-cap history); weekly at Monday 00:00 UTC on daily closes; weights drift within the week.
It is the control for A2: does CTREND add anything over plain momentum?
"""
from __future__ import annotations

from trading.replay.bars import INTERVAL_MS, Bar
from trading.replay.strategy import Signal

D = INTERVAL_MS["1d"]
W = 7 * D
MONDAY = 4 * D


class XsMomentum:
    intervals = ("1d",)
    caveats = ("A6 differs from Liu, Tsyvinski & Wu (2022): 15 coins, long-only top quintile, equal weights. "
               "See research/candidates/candidates.json A6.",)

    def __init__(self, weeks: int = 3, quantile: int = 5, btc: str = "BTCUSDT"):
        self.name = "xs_momentum"
        self.params = {"weeks": weeks, "quantile": quantile, "btc": btc, "interval": "1d",
                       "source": "Liu, Tsyvinski & Wu 2022 JF", "candidate": "A6-xs-momentum"}
        self._days, self._q, self._btc = 7 * weeks, quantile, btc
        self._seen: list[str] = []
        self.last_scores: dict[str, float] = {}

    def on_bar(self, bar: Bar, ctx) -> list[Signal]:
        if bar.symbol not in self._seen:
            self._seen.append(bar.symbol)
        if bar.symbol != self._btc or (bar.end_ms - MONDAY) % W != 0:
            return []
        t, scores, closes = bar.end_ms, {}, {}
        for sym in sorted(self._seen):
            h = ctx.history(sym, "1d", self._days + 1)
            if len(h) < self._days + 1 or h[-1].end_ms != t or h[0].close <= 0:
                continue
            scores[sym] = h[-1].close / h[0].close - 1
            closes[sym] = h[-1].close
        if len(scores) < self._q:
            return []
        self.last_scores = scores
        k = len(scores) // self._q
        top = [s for s, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]
        now, out = ctx.now_ms, []
        for sym in sorted(self._seen):
            if ctx.position(sym) is not None and sym not in top:
                out.append(Signal(self.name, sym, "close", now, "left the top momentum quintile",
                                  closes.get(sym, 0.0), "1d"))
        for sym in top:
            if ctx.position(sym) is None:
                out.append(Signal(self.name, sym, "open", now, f"top 3-week momentum quintile ({scores[sym]:+.3f})",
                                  closes[sym], "1d"))
        return out
