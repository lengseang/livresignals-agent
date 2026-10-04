"""Fallback stances from the repo's existing strategies, on real daily history. No orders, no network here.

`donchian_stances` runs the REAL `DonchianEnsemble` plugin (candidate A1) through a minimal replay: its own Context,
ReplayClock and a zero-cost PaperAccount, decisions at each daily close, fills at the next open. The stance is what the
plugin's LATEST decision targets (weight > 0 -> LONG, a zero-weight signal or none -> FLAT).

`trend_stances` is a DAILY-RESOLUTION RECONSTRUCTION of scripts/trend_follower.py's documented rule (the replay twin
`trend_real` is tied to 1m bars and 4-hourly checks, so it cannot be driven from daily bars alone):
  enter when close > SMA50(coin) AND BTC close > SMA200(BTC); stay held until the coin's trend breaks (close <= SMA50)
  or price <= entry * (1 - 15%); the BTC filter is NOT an exit. It differs from live in check cadence (daily, not 4h)
  and ignores the 3-position cap. It is a stance, not a trade signal.

Not enough history is UNAVAILABLE, never FLAT: "cannot judge" must not read as "judged: stay out".
`tsmom` and `ctrend` are not provided: they failed the repo's own gate (docs/loop/results/P1-2026-09-27.md).
"""
from __future__ import annotations

from typing import Mapping, Optional, Sequence

from trading.replay.bars import Bar, BarOpen
from trading.replay.broker import CostModel, PaperAccount
from trading.replay.context import BarStore, Context, ReplayClock
from trading.replay.plugins.donchian import DonchianEnsemble
from trading.v2.contracts import DecisionState
from trading.v2.fallback import FALLBACK_STRATEGIES

DAY_MS = 86_400_000
DONCHIAN_MIN_BARS = 400          # min_age_days 365 + a margin; below it the plugin cannot judge
TREND_COIN_MIN, TREND_BTC_MIN = 60, 205
STALE_AFTER_MS = 36 * 3_600_000


def donchian_stances(daily: Mapping[str, Sequence[Bar]], btc: str = "BTCUSDT") -> dict[str, str]:
    out = {}
    for s, w in donchian_targets(daily, btc).items():
        out[s] = "UNAVAILABLE" if w is _UNJUDGED else ("LONG" if (w is not None and w > 0) else "FLAT")
    return out


_UNJUDGED = object()


def donchian_targets(daily: Mapping[str, Sequence[Bar]], btc: str = "BTCUSDT") -> dict:
    """symbol -> the plugin's LATEST target weight: a float (0.0 = it holds the coin and is exiting), None (no signal:
    flat and staying flat), or _UNJUDGED when there is not enough/aligned history to say."""
    syms = sorted(daily)
    if btc not in daily or any(b.interval != "1d" for s in syms for b in daily[s]):
        return {s: _UNJUDGED for s in syms}
    plugin = DonchianEnsemble(btc=btc)
    store, clock = BarStore(), ReplayClock(0)
    acct = PaperAccount(1_000.0, max(1, len(syms)), CostModel(0.0, 0.0, 0.0), name="stance", mode="STANCE")
    ctx = Context(store, clock, acct)
    by_t: dict[int, list[Bar]] = {}
    nxt: dict[tuple[str, int], Bar] = {}
    for s in syms:
        bs = sorted(daily[s], key=lambda b: b.end_ms)
        for a, b in zip(bs, bs[1:]):
            nxt[(s, a.end_ms)] = b                      # the bar that OPENS when `a` closes: used for fill prices only
        for b in bs:
            by_t.setdefault(b.end_ms, []).append(b)
    last_signals: list = []
    last_t = None
    for t in sorted(by_t):
        closing = by_t[t]
        for b in closing:
            store.append(b)
        clock.advance(t)
        for b in closing:
            acct.mark(b)
        sigs = []
        for b in closing:
            sigs += plugin.on_bar(b, ctx)
        for sg in sigs:
            acct.submit(sg)
        for action in ("close", "open"):                # sales free cash before purchases, as the engine does
            for b in closing:
                n = nxt.get((b.symbol, t))
                if n is not None:
                    acct.on_open(BarOpen(n.symbol, n.interval, n.open_ms, n.open), action)
        last_signals, last_t = sigs, t
    out: dict = {}
    for s in syms:
        n = len(daily[s])
        if n < DONCHIAN_MIN_BARS or last_t is None or daily[s][-1].end_ms != last_t:
            out[s] = _UNJUDGED
            continue
        w = [sg.weight for sg in last_signals if sg.symbol == s and sg.weight is not None]
        out[s] = w[-1] if w else None
    return out


def _sma(v: Sequence[float], n: int) -> float:
    return sum(v[-n:]) / n


def trend_stances(daily: Mapping[str, Sequence[Bar]], btc: str = "BTCUSDT", sma: int = 50, btc_sma: int = 200,
                  hard_stop: float = 0.15) -> dict[str, str]:
    out: dict[str, str] = {}
    if btc not in daily:
        return {s: "UNAVAILABLE" for s in daily}
    btc_closes = {b.end_ms: b.close for b in daily[btc]}
    btc_series = [b for b in sorted(daily[btc], key=lambda b: b.end_ms)]
    for sym, bars in daily.items():
        bars = sorted(bars, key=lambda b: b.end_ms)
        if len(bars) < TREND_COIN_MIN or len(btc_series) < TREND_BTC_MIN:
            out[sym] = "UNAVAILABLE"
            continue
        held, entry = False, 0.0
        closes: list[float] = []
        btc_hist: list[float] = []
        bi = 0
        for b in bars:
            closes.append(b.close)
            while bi < len(btc_series) and btc_series[bi].end_ms <= b.end_ms:
                btc_hist.append(btc_series[bi].close)
                bi += 1
            if len(closes) < TREND_COIN_MIN:
                continue
            coin_ok = len(closes) >= max(TREND_COIN_MIN, sma) and closes[-1] > _sma(closes, sma)
            btc_ok = len(btc_hist) >= TREND_BTC_MIN and btc_hist[-1] > _sma(btc_hist, btc_sma)
            px = closes[-1]
            hard_hit = held and (px / (entry or px) - 1) <= -hard_stop
            if coin_ok and btc_ok and not held:
                held, entry = True, px
            elif held and (not coin_ok or hard_hit):
                held, entry = False, 0.0
        out[sym] = "LONG" if held else "FLAT"
    return out


class ReplayStanceSource:
    """FallbackSource (and the legacy-action source) backed by daily stances computed from real history.
    `refresh()` recomputes from the daily bars it is given (call once per UTC day); stale or missing -> UNAVAILABLE."""

    def __init__(self):
        self.donchian: dict[str, str] = {}
        self.trend: dict[str, str] = {}
        self.refreshed_ms: Optional[int] = None

    def refresh(self, daily: Mapping[str, Sequence[Bar]], now_ms: int) -> None:
        self.donchian = donchian_stances(daily)
        self.trend = trend_stances(daily)
        self.refreshed_ms = now_ms

    def _fresh(self, now_ms: Optional[int]) -> bool:
        return self.refreshed_ms is not None and (now_ms is None or now_ms - self.refreshed_ms <= STALE_AFTER_MS)

    def stances(self, state: DecisionState, now_ms: Optional[int] = None) -> Mapping[str, str]:
        if not self._fresh(now_ms if now_ms is not None else state.timestamp * 1000):
            return {n: "UNAVAILABLE" for n in FALLBACK_STRATEGIES}
        return {"trend_follower": self.trend.get(state.symbol, "UNAVAILABLE"),
                "donchian_ensemble": self.donchian.get(state.symbol, "UNAVAILABLE"),
                "tsmom": "UNAVAILABLE", "ctrend": "UNAVAILABLE"}

    def legacy_action(self, symbol: str, position_side: str, now_ms: int) -> Optional[str]:
        """What the CURRENT system's rule would do now: trend_follower's stance mapped through the position."""
        st = self.trend.get(symbol)
        if st not in ("LONG", "FLAT") or not self._fresh(now_ms):
            return None
        if st == "LONG":
            return {"none": "LONG", "long": "HOLD", "short": "EXIT"}[position_side]
        return "NO_TRADE" if position_side == "none" else "EXIT"
