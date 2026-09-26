"""Regime-adaptive long/short (strategy-audit #9). REPLAY/PAPER only; SHORTS ARE A PAPER-ONLY SIMULATION.

Port of the rule documented in scripts/regime_trader.py (read-only; neither imported nor edited), fixed in
docs/loop/designs/T3.4.md before any run. Once per UTC day, on BTC's daily close (every symbol's daily bar for
that day is already in the store: same-instant fairness):
  regime  bull if close(BTC) > SMA_R(BTC), else bear;
  exits   a long closes if close < SMA50, a short if close > SMA50; broker: time stop, optional sl/tp;
  entries only into slots free BEFORE today's exits:
          bull -> coins with close > SMA50, strongest 30-day return first, LONG;
          bear -> coins with close < SMA50, weakest 30-day return first, SHORT (paper only).
Differences from the live script: equal slots (no ATR stop / risk parity), no breadth or BTC-momentum gate,
no gold slot, no macro filter, Binance spot history instead of Bybit linear.
"""
from __future__ import annotations

from trading.replay.bars import INTERVAL_MS, Bar
from trading.replay.strategy import Signal

D = INTERVAL_MS["1d"]


class RegimeLS:
    intervals = ("1d",)
    caveats = ("PAPER-ONLY shorts: spot cannot short. Short P&L is simulated with no borrow/funding cost "
               "(UNKNOWN, either sign on perpetuals) and no margin call or liquidation.",
               "Port differs from scripts/regime_trader.py: equal slots, no ATR stop/risk parity, no breadth/"
               "BTC-momentum gate, no gold slot, no macro filter; Binance spot data, not Bybit linear.")

    def __init__(self, sma_regime: int = 200, sma_trend: int = 50, mom_days: int = 30, topn: int = 3,
                 max_hold_days: int = 21, sl: float | None = None, tp: float | None = None,
                 btc: str = "BTCUSDT"):
        self.name = "regime_ls"
        self.params = {"sma_regime": sma_regime, "sma_trend": sma_trend, "mom_days": mom_days, "topn": topn,
                       "max_hold_days": max_hold_days, "sl": sl, "tp": tp, "btc": btc, "interval": "1d",
                       "shorts": "PAPER-ONLY"}
        self._r, self._t, self._m, self._top = sma_regime, sma_trend, mom_days, topn
        self._hold, self._sl, self._tp, self._btc = max_hold_days * D, sl, tp, btc
        self._seen: list[str] = []

    def _coin(self, ctx, sym: str, end_ms: int):
        """(close, sma_trend, mom) from this same day's bar, or None (no fresh bar / warmup)."""
        n = max(self._t, self._m + 1)
        h = ctx.history(sym, "1d", n)
        if len(h) < n or h[-1].end_ms != end_ms:
            return None
        c = [b.close for b in h]
        return c[-1], sum(c[-self._t:]) / self._t, c[-1] / c[-1 - self._m] - 1

    def on_bar(self, bar: Bar, ctx) -> list[Signal]:
        if bar.symbol not in self._seen:
            self._seen.append(bar.symbol)
        if bar.symbol != self._btc:
            return []
        b = ctx.history(self._btc, "1d", self._r)
        if len(b) < self._r:
            return []
        bear = b[-1].close < sum(x.close for x in b) / self._r
        now, out, held, cands = ctx.now_ms, [], 0, []
        for sym in sorted(self._seen):
            pos = ctx.position(sym)
            info = self._coin(ctx, sym, bar.end_ms)
            if pos is not None:
                held += 1
                if info is None:
                    continue
                close, sma, _ = info
                short = pos[0] < 0
                if (short and close > sma) or (not short and close < sma):
                    out.append(Signal(self.name, sym, "close", now, "trend-broke", close, "1d",
                                      side="S" if short else "L"))
                continue
            if info is None:
                continue
            close, sma, mom = info
            if (bear and close < sma) or (not bear and close > sma):
                cands.append((mom, sym, close))
        free = self._top - held                         # slots free BEFORE today's exits
        if free > 0 and cands:
            # bull: strongest 30-day return first; bear: weakest first (ties by symbol, deterministic)
            cands.sort(key=(lambda x: (x[0], x[1])) if bear else (lambda x: (-x[0], x[1])))
            side = "S" if bear else "L"
            for mom, sym, close in cands[:free]:
                out.append(Signal(self.name, sym, "open", now,
                                  f"{'bear' if bear else 'bull'} (BTC vs SMA{self._r}); mom{self._m}d={mom:+.3f}"
                                  + ("; PAPER-ONLY short" if bear else ""), close, "1d", side=side,
                                  sl=self._sl, tp=self._tp, max_hold_ms=self._hold))
        return out
