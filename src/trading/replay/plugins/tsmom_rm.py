"""Risk-managed time-series momentum (candidate A3). REPLAY/PAPER only.

Re-implementation of Howden & Andreev (SSRN 7115459, 2026) from its abstract, rules recorded in
research/candidates/candidates.json (A3) before any run:
  assets BTC, ETH, BNB, XRP, ADA, SOL, DOGE;
  hold an asset only while its trailing 30-day return is positive, else cash (long/cash);
  scale gross exposure down 50% while the aggregate crypto market is more than 15% below its peak.
Declared deviations (not in the abstract, fixed by us before any result): equal weight across held assets,
daily rebalance at the next open (target weights, P1c); aggregate market = equal-weight index of the same
assets (daily returns averaged over the assets with a bar), peak = running max of that index.
"""
from __future__ import annotations

from trading.replay.bars import INTERVAL_MS, Bar
from trading.replay.strategy import Signal

D = INTERVAL_MS["1d"]
ASSETS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "SOLUSDT", "DOGEUSDT")


class TsmomRiskManaged:
    intervals = ("1d",)
    caveats = ("A3 from the SSRN abstract only (full paper blocked): equal weights, daily rebalance and the "
               "equal-weight market index are our declared choices. See research/candidates/candidates.json A3.",)

    def __init__(self, lookback_days: int = 30, drawdown: float = 0.15, scale: float = 0.5,
                 assets: tuple = ASSETS, btc: str = "BTCUSDT"):
        self.name = "tsmom_risk_managed"
        self.params = {"lookback_days": lookback_days, "drawdown": drawdown, "scale": scale,
                       "assets": list(assets), "interval": "1d", "candidate": "A3-tsmom-risk-managed"}
        self._n, self._dd, self._k, self._assets, self._btc = lookback_days, drawdown, scale, tuple(assets), btc
        self._index, self._peak = 1.0, 1.0
        self._last: dict[str, float] = {}
        self._weights: dict[str, float] = {}

    def on_bar(self, bar: Bar, ctx) -> list[Signal]:
        if bar.symbol != self._btc:                     # decide once a day, on BTC's daily close
            return []
        t, rets, want = bar.end_ms, [], []
        for sym in self._assets:
            h = ctx.history(sym, "1d", self._n + 1)
            if not h or h[-1].end_ms != t:
                continue
            c = h[-1].close
            if sym in self._last and self._last[sym] > 0:
                rets.append(c / self._last[sym] - 1)
            self._last[sym] = c
            if len(h) == self._n + 1 and c / h[0].close - 1 > 0:
                want.append(sym)
        if rets:
            self._index *= 1 + sum(rets) / len(rets)
            self._peak = max(self._peak, self._index)
        stressed = self._index < (1 - self._dd) * self._peak
        gross = self._k if stressed else 1.0
        w = gross / len(want) if want else 0.0
        target = {s: (w if s in want else 0.0) for s in self._assets}
        now, out = ctx.now_ms, []
        for sym, wt in target.items():
            held = ctx.position(sym) is not None
            if wt == 0.0 and not held:
                continue
            why = (f"30d return {'>' if wt else '<='} 0" + ("; market >15% off peak: exposure x0.5" if stressed else ""))
            out.append(Signal(self.name, sym, "target", now, why, self._last.get(sym, 0.0), "1d", weight=wt))
        self._weights = target
        return out
