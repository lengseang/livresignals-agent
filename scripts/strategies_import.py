#!/usr/bin/env python3
"""Imported PUBLIC strategies — run PAPER-PARALLEL for learning, promote winners.

Owner request: copy other people's strategies and trade them in parallel.
These are the four most-followed published strategies, implemented from their
public formulas (pure python, no pandas):

  1. SUPERTREND   — ATR-based trend follower (TradingView's most-added indicator)
  2. RSI(2)       — Connors' famous short-term mean reversion ("buy the 2-RSI dip")
  3. BOLLINGER    — classic band mean-reversion (buy lower band, sell middle)
  4. EMA-CROSS    — trend following on 20/50 EMA cross

Every strategy runs PAPER-ONLY in parallel (same 10 symbols x 3 TFs). The
improvement_loop tracks each strategy's win rate by its `edge` tag and promotes
(real money) only strategies that PROVE >=58% WR over 10+ trades. This is the
safe way to copy others: filter through our own market before risking $.

Each signal function returns a dict {ok, close, reason} or {"ok": False}.
"""
from __future__ import annotations


# ---------------------------------------------------------------- indicators
def _ema(vals: list[float], n: int) -> float:
    if not vals:
        return 0.0
    k = 2 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def _ema_series(vals: list[float], n: int) -> list[float]:
    if not vals:
        return []
    k = 2 / (n + 1)
    e = vals[0]
    out = [e]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(-period, 0):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100 - 100 / (1 + rs)


def _atr(ohlcv: list[list], period: int = 14) -> float:
    if len(ohlcv) < period + 1:
        return 0.0
    trs = []
    for i in range(-period, 0):
        h, l, c = ohlcv[i][2], ohlcv[i][3], ohlcv[i - 1][4]
        trs.append(max(h - l, abs(h - c), abs(l - c)))
    return sum(trs) / len(trs)


# ---------------------------------------------------------------- strategies
def supertrend_signal(ohlcv: list[list]) -> dict:
    """Supertrend(10,3): buy when price crosses ABOVE the band (trend flipped up).

    KILLED 2026-09-09 (auto-improve pass): 72h paper WR 37.5% / n=120 / net
    -$0.758 — meets the disable bar (WR < 42% AND negative net), flagged by
    night_tuner 09-09 (flaw + rank-2 KILL) and improvement-report 09-08
    (edge_decay). Same treatment as pullback (killed 09-06). Imported track is
    paper-only so no real money was ever exposed. Revive only after a
    walk-forward re-fit shows >=55% WR over 50+ fresh paper trades.
    """
    return {"ok": False}


def _supertrend_was_up(ohlcv, atr, mult, hl2) -> bool:
    closes = [c[4] for c in ohlcv]
    i = len(ohlcv) - 2
    basic_dn = hl2[i] + mult * atr
    return closes[i] > basic_dn


def rsi2_signal(ohlcv: list[list]) -> dict:
    """Connors RSI(2): buy when 2-period RSI < 10 (panic oversold) — mean revert.

    KILLED 2026-09-09 (auto-improve pass): 72h paper WR 26.7% / n=15 / net
    -$0.160 — meets the disable bar (WR < 42% AND negative net). Was 61.9% WR /
    +$0.15 all-time through 09-07 (promotion candidate in the 09-08 report) but
    collapsed: last-72h truth is a bleed; night_tuner 09-09 flags it (flaw +
    rank-2 KILL). Imported track is paper-only so no real money was ever
    exposed. Revive only after >=55% WR over 50+ fresh paper trades.
    """
    return {"ok": False}


def bollinger_signal(ohlcv: list[list]) -> dict:
    """Bollinger(20,2): buy when close < lower band AND price above 200-EMA trend.

    KILLED 2026-09-11 (auto-improve pass): last-72h paper WR 29.8% / n=47 /
    net -$0.662 — meets the disable bar (WR < 42% AND negative net) on n>=8.
    Flagged by night_tuner 2026-09-11 (flaw + rank-2 "KILL or pause bollinger")
    and improvement-report 2026-09-10 (edge_decay: 35.3% WR / -0.20 net).
    Paper-only imported track, so no real money was ever exposed. Same
    treatment as pullback (killed 09-06), supertrend + rsi2 (killed 09-09).
    Revive only after a walk-forward re-fit shows >=55% WR over 50+ fresh
    paper trades.
    """
    return {"ok": False}


def ema_cross_signal(ohlcv: list[list]) -> dict:
    """EMA 20/50 cross: buy when fast EMA crosses ABOVE slow EMA (momentum up)."""
    if len(ohlcv) < 60:
        return {"ok": False}
    closes = [c[4] for c in ohlcv]
    ema20 = _ema_series(closes, 20)
    ema50 = _ema_series(closes, 50)
    # cross just happened: prev fast<=slow, now fast>slow
    if ema20[-1] > ema50[-1] and ema20[-2] <= ema50[-2]:
        return {"ok": True, "close": closes[-1], "reason": "EMA20 crossed above EMA50"}
    return {"ok": False}


def breakout_signal(ohlcv: list[list]) -> dict:
    """Breakout: close makes a NEW 20-bar high (trend-following long).

    Added 2026-09-10 per owner request to test non-dip signals. Backtest on
    1mo 15m data gave 52.6% (n=734) — marginal, so PAPER-ONLY until proven
    live (>=58% over 20+ paper trades).
    """
    if len(ohlcv) < 60:
        return {"ok": False}
    highs = [c[2] for c in ohlcv]
    closes = [c[4] for c in ohlcv]
    prior_high = max(highs[-21:-1])          # highest high of the 20 bars before now
    if closes[-1] > prior_high:
        return {"ok": True, "close": closes[-1],
                "reason": f"new 20-bar high ({closes[-1]:.4f} > {prior_high:.4f})"}
    return {"ok": False}


def momentum_signal(ohlcv: list[list]) -> dict:
    """Momentum continuation: 3 consecutive green closes AND RSI(14) > 60.

    Backtest 51.9% (n=1203) — marginal, PAPER-ONLY until proven live.
    """
    if len(ohlcv) < 60:
        return {"ok": False}
    closes = [c[4] for c in ohlcv]
    green3 = closes[-1] > closes[-2] > closes[-3] > closes[-4]
    r = _rsi(closes, 14)
    if green3 and r > 60:
        return {"ok": True, "close": closes[-1],
                "reason": f"3 green + RSI {r:.0f}"}
    return {"ok": False}


STRATEGIES = {
    "supertrend": supertrend_signal,
    "rsi2": rsi2_signal,
    "bollinger": bollinger_signal,
    "ema-cross": ema_cross_signal,
    "breakout": breakout_signal,
    "momentum": momentum_signal,
}


def scan_strategy(ex, sym: str, tf: str, name: str) -> dict:
    """Fetch OHLCV and run one named strategy. Returns {ok, close, reason}."""
    fn = STRATEGIES.get(name)
    if not fn:
        return {"ok": False}
    try:
        ohlcv = ex.fetch_ohlcv(sym, tf, limit=150)
    except Exception:
        return {"ok": False}
    if not ohlcv or len(ohlcv) < 60:
        return {"ok": False}
    sig = fn(ohlcv)
    if sig.get("ok"):
        sig["symbol"] = sym
        sig["tf"] = tf
        sig["edge"] = name
    return sig


def all_strategies(ex, sym: str, tf: str) -> list[dict]:
    """Run all 4 imported strategies on one (sym,tf); return list of fired signals."""
    out = []
    try:
        ohlcv = ex.fetch_ohlcv(sym, tf, limit=150)
    except Exception:
        return out
    if not ohlcv or len(ohlcv) < 60:
        return out
    for name, fn in STRATEGIES.items():
        try:
            sig = fn(ohlcv)
        except Exception:
            continue
        if sig.get("ok"):
            sig["symbol"] = sym
            sig["tf"] = tf
            sig["edge"] = name
            sig["close"] = float(ohlcv[-1][4])
            out.append(sig)
    return out
