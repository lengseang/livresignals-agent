"""trend_real: scripts/trend_follower.py's REAL-money book as it actually trades (REPLAY/paper only).

Read-only port; the original is never imported, run or edited. Rules and their line numbers in
scripts/trend_follower.py are listed in docs/loop/designs/T3.3c.md (R1-R13). In short:

* universe = the 15 REAL_SYMS (:48-50); one check every 4 h at minute 10 UTC (crontab.backup:41);
* series = completed daily closes + the forming day's candle, so closes[-1] is the current price (:190, :213);
* a coin with < 60 daily bars is skipped entirely (:211); BTC needs >= 205 bars for the regime (:92);
* entry (edge-triggered on the per-coin paper state, :226): close > SMA_N(coin) AND BTC > SMA200(BTC) (:90-95);
* exit: the coin's own trend breaks, or price <= paper entry * (1 - 0.15) (:223-235); the BTC filter is NOT an exit;
* real buys: this check's candidates by 30-day momentum, descending, while real open < MAX_POS = 3 (:230, :282-290).

The real book is read from the account (ctx.position), never kept here, so it cannot drift from the fills.
"""
from __future__ import annotations

from trading.replay.bars import INTERVAL_MS, Bar
from trading.replay.strategy import Signal

REAL_UNIVERSE = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT", "LINKUSDT", "DOGEUSDT",
                 "AVAXUSDT", "NEARUSDT", "SUIUSDT", "ARBUSDT", "DOTUSDT", "LTCUSDT", "ATOMUSDT")
DAY = INTERVAL_MS["1d"]


def _sma(v: list[float], n: int) -> float:
    return sum(v[-n:]) / n


class TrendReal:
    intervals = ("1m", "1d")

    def __init__(self, sma: int = 50, hard_stop: float | None = 0.15, max_pos: int = 3, btc: str = "BTCUSDT",
                 universe: tuple[str, ...] = REAL_UNIVERSE, btc_sma: int = 200, min_bars: int = 60,
                 btc_min_bars: int = 205, mom_days: int = 30, max_daily_bars: int = 300,
                 check_every_h: int = 4, check_offset_min: int = 10):
        self.name = "trend_real"
        self.params = {"sma": sma, "btc_sma": btc_sma, "hard_stop": hard_stop, "max_pos": max_pos,
                       "min_bars": min_bars, "btc_min_bars": btc_min_bars, "mom_days": mom_days,
                       "max_daily_bars": max_daily_bars, "check_every_h": check_every_h,
                       "check_offset_min": check_offset_min, "btc": btc, "universe": list(universe)}
        self._n, self._stop, self._max, self._btc = sma, hard_stop, max_pos, btc
        self._universe, self._btc_sma = tuple(universe), btc_sma
        # variants with SMA_N > 60 need N bars (the live code would divide a short sum by N; see T3.3c.md)
        self._coin_min, self._btc_min = max(min_bars, sma), btc_min_bars
        self._skip_min, self._mom, self._maxd = min_bars, mom_days, max_daily_bars
        self._period, self._off = check_every_h * 3_600_000, check_offset_min * 60_000
        self._next: int | None = None
        self._paper: dict[str, tuple[bool, float]] = {}     # symbol -> (held, entry price), live `pos` state
        self.checks = 0

    # --- check clock ----------------------------------------------------------------------------
    def _grid_at_or_after(self, t: int) -> int:
        k = -(-(t - self._off) // self._period)
        return k * self._period + self._off

    def _due(self, t: int) -> bool:
        if self._next is None:
            self._next = self._grid_at_or_after(t)
        if t < self._next:
            return False
        self._next = self._grid_at_or_after(t + 1)
        return True

    # --- data as the live fetch_ohlcv(sym, "1d", limit=300) returns it -----------------------------
    def _closes(self, ctx, symbol: str, day_start: int) -> list[float] | None:
        last = ctx.last(symbol, "1m")
        if last is None or last.end_ms <= day_start:
            return None                                  # no print today: no forming daily candle
        daily = [b.close for b in ctx.history(symbol, "1d", self._maxd - 1) if b.end_ms <= day_start]
        return daily + [last.close]

    def on_bar(self, bar: Bar, ctx) -> list[Signal]:
        if bar.interval != "1m" or bar.symbol != self._btc or not self._due(bar.end_ms):
            return []
        self.checks += 1
        now = ctx.now_ms
        day_start = now - now % DAY
        btc = self._closes(ctx, self._btc, day_start)
        if not btc:
            return []                                    # no BTC data: the live run aborts (:195-197)
        btc_ok = len(btc) >= self._btc_min and btc[-1] > _sma(btc, self._btc_sma)

        out: list[Signal] = []
        candidates: list[tuple[float, str, float]] = []
        closing: set[str] = set()
        for sym in self._universe:
            c = self._closes(ctx, sym, day_start)
            if c is None or len(c) < self._skip_min:
                continue
            px = c[-1]
            coin_ok = len(c) >= self._coin_min and px > _sma(c, self._n)
            held, entry = self._paper.get(sym, (False, 0.0))
            hard_hit = held and self._stop is not None and (px / (entry or px) - 1) <= -self._stop
            if coin_ok and btc_ok and not held:
                self._paper[sym] = (True, px)
                mom = c[-1] / c[-(self._mom + 1)] - 1 if len(c) > self._mom + 1 else 0.0
                candidates.append((mom, sym, px))
            elif (held and not coin_ok) or hard_hit:
                self._paper[sym] = (False, 0.0)
                if ctx.position(sym) is not None:
                    why = "HARD-STOP" if hard_hit else "trend-broke"
                    out.append(Signal(self.name, sym, "close", now, why, px, "1m"))
                    closing.add(sym)

        n_open = sum(1 for s in self._universe if ctx.position(s) is not None and s not in closing)
        candidates.sort(reverse=True)
        for mom, sym, px in candidates:
            if n_open >= self._max:
                break
            out.append(Signal(self.name, sym, "open", now,
                              f"close>sma{self._n} & btc>sma{self._btc_sma}; mom{self._mom}d {mom:+.3f}", px, "1m"))
            n_open += 1
        return out
