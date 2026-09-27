"""Benchmark 'strategies' for the shadow scanner's benchmark lane (P2 design §2). REPLAY/PAPER only.

buy_hold_btc: BTC at weight 1 from the first daily close, held forever.
buy_hold_ew:  every universe coin at weight 1/N from the first daily close it has, never rebalanced.
They run through the same fill model as the candidates so forward comparisons share costs. (The replay gate's
benchmarks are the separate bh_* accounts of P0b; these exist so the live league has the same bar.)
"""
from __future__ import annotations

from trading.replay.bars import Bar
from trading.replay.strategy import Signal


class BuyHoldBTC:
    intervals = ("1d",)
    caveats = ()

    def __init__(self, btc: str = "BTCUSDT"):
        self.name, self.params, self._btc, self._done = "buy_hold_btc", {"btc": btc}, btc, False

    def on_bar(self, bar: Bar, ctx) -> list[Signal]:
        if self._done or bar.symbol != self._btc:
            return []
        self._done = True
        return [Signal(self.name, self._btc, "target", ctx.now_ms, "benchmark: buy and hold", bar.close, "1d",
                       weight=1.0)]


class BuyHoldEW:
    intervals = ("1d",)
    caveats = ()

    def __init__(self, universe: tuple = ()):
        self.name, self.params = "buy_hold_ew", {"universe": list(universe)}
        self._n, self._bought = max(1, len(universe)), set()
        self._universe = set(universe)

    def on_bar(self, bar: Bar, ctx) -> list[Signal]:
        if bar.symbol in self._bought or (self._universe and bar.symbol not in self._universe):
            return []
        self._bought.add(bar.symbol)
        # 1/N of equity at the fill. Shadow priming starts after every universe coin listed, so all N are bought
        # at the same first close (exact equal weight); a later listing would get 1/N of the equity then.
        w = 1.0 / self._n
        return [Signal(self.name, bar.symbol, "target", ctx.now_ms, "benchmark: equal-weight hold", bar.close, "1d",
                       weight=w)]
