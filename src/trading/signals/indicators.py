"""Pure indicator functions: ATR14, ADX14, EMA20/50, realized vol.

All functions are pure: (closed rows) -> series. No state, no lookahead.
Rows are dicts with open/high/low/close keys, ordered by open_time_utc.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence


def _closes(rows: Sequence[dict]) -> list[float]:
    return [float(r["close"]) for r in rows]


def ema(values: Sequence[float], period: int) -> list[float]:
    """Exponential moving average seeded with SMA of the first `period` values."""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out: list[float] = []
    prev = seed
    for i, v in enumerate(values):
        if i < period - 1:
            continue
        if i == period - 1:
            cur = seed
        else:
            cur = v * k + prev * (1 - k)
        out.append(cur)
        prev = cur
    return out


def ema_series(rows: Sequence[dict], period: int) -> list[float]:
    return ema(_closes(rows), period)


def true_ranges(rows: Sequence[dict]) -> list[float]:
    trs: list[float] = []
    for i, r in enumerate(rows):
        h, l = float(r["high"]), float(r["low"])
        if i == 0:
            trs.append(h - l)
            continue
        pc = float(rows[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return trs


def atr(rows: Sequence[dict], period: int = 14) -> list[float]:
    """Wilder-smoothed ATR, aligned so out[i] corresponds to rows[period-1+i]."""
    trs = true_ranges(rows)
    if len(trs) < period:
        return []
    seed = sum(trs[:period]) / period
    out: list[float] = [seed]
    prev = seed
    for tr in trs[period:]:
        prev = (prev * (period - 1) + tr) / period
        out.append(prev)
    return out


def _directional_movement(rows: Sequence[dict]) -> tuple[list[float], list[float]]:
    plus, minus = [], []
    for i in range(1, len(rows)):
        up = float(rows[i]["high"]) - float(rows[i - 1]["high"])
        down = float(rows[i - 1]["low"]) - float(rows[i]["low"])
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
    return plus, minus


def _wilder_smooth(values: Sequence[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    seed = sum(values[:period])
    out: list[float] = [seed]
    prev = seed
    for v in values[period:]:
        prev = prev - prev / period + v
        out.append(prev)
    return out


def adx(rows: Sequence[dict], period: int = 14) -> list[float]:
    """Wilder ADX aligned to rows[2*period-2+i]. Returns [] if insufficient rows."""
    if len(rows) < 2 * period:
        return []
    trs = true_ranges(rows)
    atr_sm = _wilder_smooth(trs, period)  # len(rows)-period+1 entries
    plus_dm, minus_dm = _directional_movement(rows)
    plus_sm = _wilder_smooth(plus_dm, period)
    minus_sm = _wilder_smooth(minus_dm, period)

    dx: list[float] = []
    for a, p, m in zip(atr_sm, plus_sm, minus_sm):
        if a <= 0 or (p + m) <= 0:
            dx.append(0.0)
            continue
        dx.append(100.0 * abs(p - m) / (p + m))

    # ADX = Wilder-smoothed DX; needs a second smoothing window.
    if len(dx) < period:
        return []
    seed = sum(dx[:period]) / period
    out: list[float] = [seed]
    prev = seed
    for v in dx[period:]:
        prev = (prev * (period - 1) + v) / period
        out.append(prev)
    return out


def realized_vol(rows: Sequence[dict], window: int = 20) -> list[float]:
    """Annualized realized volatility from log returns (per-bar, not scaled)."""
    closes = _closes(rows)
    if len(closes) < window + 1:
        return []
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    out: list[float] = []
    for i in range(window - 1, len(rets)):
        window_ret = rets[i - window + 1 : i + 1]
        mean = sum(window_ret) / window
        var = sum((x - mean) ** 2 for x in window_ret) / (window - 1)
        out.append(math.sqrt(max(var, 0.0)))
    return out


def indicator_snapshot(rows: Sequence[dict]) -> dict:
    """Latest values of all indicators for the given closed rows."""
    closes = _closes(rows)
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    a = atr(rows, 14)
    ax = adx(rows, 14)
    rv = realized_vol(rows, 20)
    return {
        "close": closes[-1] if closes else None,
        "ema20": ema20[-1] if ema20 else None,
        "ema50": ema50[-1] if ema50 else None,
        "atr14": a[-1] if a else None,
        "adx14": ax[-1] if ax else None,
        "realized_vol20": rv[-1] if rv else None,
    }