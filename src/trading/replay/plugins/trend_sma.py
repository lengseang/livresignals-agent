"""Baseline: the trend_follower signal, re-implemented from its documented rule (REPLAY/paper only).

Rule (docs/research/strategy-inventory.md): on each daily close of coin X,
  enter long  if close(X) > SMA_n(X) and close(BTC) > SMA_m(BTC) and X is not held;
  exit        if close(X) < SMA_n(X).
scripts/trend_follower.py is neither imported nor edited. Known differences from it: no -15% hard
stop, daily closes instead of a 4-hourly check, and sizing comes from the shared replay account.
"""
from __future__ import annotations

from trading.replay.bars import Bar
from trading.replay.strategy import Signal


class TrendSMA:
    intervals = ("1d",)

    def __init__(self, sma: int = 50, btc_sma: int = 50, btc: str = "BTCUSDT"):
        self.name = "trend_sma"
        self.params = {"sma": sma, "btc_sma": btc_sma, "btc": btc, "interval": "1d"}
        self._n, self._m, self._btc = sma, btc_sma, btc

    @staticmethod
    def _above(bars: tuple[Bar, ...], n: int) -> tuple[float, float] | None:
        if len(bars) < n:
            return None
        return bars[-1].close, sum(b.close for b in bars) / n

    def on_bar(self, bar: Bar, ctx) -> list[Signal]:
        coin = self._above(ctx.history(bar.symbol, "1d", self._n), self._n)
        if coin is None:
            return []
        close, sma = coin
        held = ctx.position(bar.symbol) is not None
        if held:
            if close < sma:
                return [Signal(self.name, bar.symbol, "close", ctx.now_ms, f"close<sma{self._n}", close, "1d")]
            return []
        btc_bars = ctx.history(self._btc, "1d", self._m)
        if not btc_bars or btc_bars[-1].end_ms != bar.end_ms:
            return []                                   # no BTC bar for this same day: no regime reading
        btc = self._above(btc_bars, self._m)
        if btc is None:
            return []
        if close > sma and btc[0] > btc[1]:
            return [Signal(self.name, bar.symbol, "open", ctx.now_ms,
                           f"close>sma{self._n} & btc>sma{self._m}", close, "1d")]
        return []
