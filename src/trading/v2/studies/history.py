"""Study data: Binance 1m history -> numpy minute arrays + the REAL features-v1 snapshot at every hour.

Features are computed by `trading.v2.features.compute_features`, the same function the live pipeline runs, on
the same closed bars, so a model fitted here sees exactly the numbers it will see live. History has no order
book, so a replay book is supplied (close +- PAPER half-spread, equal quantities): the four orderbook features
are therefore NOT information in a study and are excluded from every model input (see BOOK_FEATURES).

Cached per symbol as `<cache>/<SYM>.npz`; rebuilt if absent. Research only: no order path, no network.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Iterable

import numpy as np

from trading.replay.bars import Bar
from trading.replay.context import BarStore
from trading.replay.loader import iter_symbol
from trading.replay.resample import Resampler
from trading.v2.features import Book, compute_features

HALF_SPREAD_BPS = 0.41          # approved paper spread 0.82 bps / 2
BOOK_FEATURES = ("bid_depth", "ask_depth", "imbalance", "spread_bps")

# model inputs: every bar-derived features-v1 number (book features excluded, regime one-hot)
FEATURE_COLUMNS = (
    "ret_1m", "ret_5m", "ret_15m", "ret_1h", "ret_4h",
    "rsi", "atr", "ema_fast_slope", "ema_slow_slope", "distance_vwap", "volume_zscore",
    "trend_bullish", "trend_bearish", "vol_high",
)


class _ReplayBook:
    def __init__(self):
        self.book: Book | None = None

    def latest(self, symbol: str):
        return self.book


def _row(snap) -> list[float]:
    d = snap.to_dict()
    r, t, v, g = d["returns"], d["technical"], d["volume"], d["regime"]
    return [r["1m"], r["5m"], r["15m"], r["1h"], r["4h"],
            t["rsi"], t["atr"], t["ema_fast_slope"], t["ema_slow_slope"], t["distance_vwap"], v["volume_zscore"],
            1.0 if g["trend"] == "bullish" else 0.0, 1.0 if g["trend"] == "bearish" else 0.0,
            1.0 if g["volatility"] == "high" else 0.0]


def build(root: Path, symbol: str, every_ms: int = 3_600_000) -> dict:
    """One pass over the symbol's 1m history."""
    store, rs, book = BarStore(keep_1m=400), Resampler(symbol, "1h"), _ReplayBook()
    m_open, m_o, m_h, m_l, m_c = [], [], [], [], []
    h_end, h_h, h_l, h_c = [], [], [], []
    f_ts, f_close, f_rows, f_missing = [], [], [], 0
    for bar in iter_symbol(root, symbol):
        for hb in rs.add(bar):              # the previous hour closed: it is known from hb.end_ms on
            store.append(hb)
            h_end.append(hb.end_ms); h_h.append(hb.high); h_l.append(hb.low); h_c.append(hb.close)
        store.append(bar)
        m_open.append(bar.open_ms); m_o.append(bar.open); m_h.append(bar.high); m_l.append(bar.low); m_c.append(bar.close)
        if bar.end_ms % every_ms:
            continue
        hb = rs.flush_until(bar.end_ms)     # the hour that closes with this minute
        if hb is not None:
            store.append(hb)
            h_end.append(hb.end_ms); h_h.append(hb.high); h_l.append(hb.low); h_c.append(hb.close)
        hs = HALF_SPREAD_BPS / 1e4
        book.book = Book(bar.end_ms, bar.close * (1 - hs), bar.close * (1 + hs), 1.0, 1.0)
        res = compute_features(symbol, store, book, bar.end_ms)
        if not res.ok:
            f_missing += 1
            continue
        f_ts.append(bar.end_ms); f_close.append(bar.close); f_rows.append(_row(res.snapshot))
    return {
        "m_open": np.array(m_open, dtype=np.int64), "m_o": np.array(m_o), "m_h": np.array(m_h),
        "m_l": np.array(m_l), "m_c": np.array(m_c),
        "h_end": np.array(h_end, dtype=np.int64), "h_h": np.array(h_h), "h_l": np.array(h_l), "h_c": np.array(h_c),
        "f_ts": np.array(f_ts, dtype=np.int64), "f_close": np.array(f_close),
        "f_x": np.array(f_rows, dtype=float).reshape(-1, len(FEATURE_COLUMNS)),
        "f_missing": np.array(f_missing),
    }


def load(root: Path, cache: Path, symbol: str) -> dict:
    cache.mkdir(parents=True, exist_ok=True)
    p = cache / f"{symbol}.npz"
    if not p.exists():
        np.savez(p, **build(root, symbol))
    with np.load(p) as z:
        return {k: z[k] for k in z.files}


def wilder_atr_frac(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder ATR(period) / close per hourly bar (NaN until warm). Same definition as features-v1's `atr`."""
    n = len(c)
    out = np.full(n, np.nan)
    if n <= period:
        return out
    tr = np.empty(n)
    tr[0] = h[0] - l[0]
    prev = c[:-1]
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - prev), np.abs(l[1:] - prev)))
    a = tr[1:period + 1].mean()
    out[period] = a / c[period]
    for i in range(period + 1, n):
        a = (a * (period - 1) + tr[i]) / period
        out[i] = a / c[i]
    return out


def is_finite_rows(x: np.ndarray) -> np.ndarray:
    return np.all(np.isfinite(x), axis=1)


def utc_ms(y: int, m: int, d: int = 1) -> int:
    from datetime import datetime, timezone
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def sanity(d: dict) -> Iterable[str]:
    """What a reader should know about the cached data."""
    yield f"minutes={len(d['m_open'])} hours={len(d['h_end'])} feature_rows={len(d['f_ts'])} missing={int(d['f_missing'])}"
    if len(d["f_ts"]):
        yield f"features {d['f_ts'][0]} .. {d['f_ts'][-1]}"
    assert math.isfinite(float(d["m_c"].sum()))
