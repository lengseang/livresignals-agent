"""Normalization: closed-candle enforcement, UTC, dedup keys, gap detection."""
from __future__ import annotations

import time

import ccxt

from trading.config import FRESHNESS_DEADLINES


def normalize_candle(raw, symbol: str, timeframe: str, source: str = "binance.vision",
                     now_ms: int | None = None) -> dict | None:
    """Convert a CCXT OHLCV raw row into a normalized candle dict.

    Returns None if the candle is still open (not final per data-contract §4).
    `now_ms` is the caller's clock (tests and replays pass their own).
    """
    if not raw or isinstance(raw, (str, int, float)):
        return None
    ts, open_v, high_v, low_v, close_v, volume = raw[:6]
    if not isinstance(ts, int):
        return None

    interval_ms = _interval_ms(timeframe)
    close_time = ts + interval_ms - 1
    if len(raw) > 6 and raw[6] is not None and int(raw[6]) != close_time:
        # A supplied close_time that disagrees with (open + timeframe) means the
        # row's timeframe is not the one it is being stored under. Guessing
        # here would silently mislabel a bar; the caller must fix the pair.
        raise ValueError(
            f"{symbol} {timeframe}: raw close_time {raw[6]} != open {ts} + {interval_ms}ms")
    # Closed-candle rule: only use when the interval has ended.
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if now_ms < close_time:
        return None  # open candle, not usable

    received = now_ms
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "open_time_utc": ts,
        "close_time_utc": close_time,
        "open": float(open_v),
        "high": float(high_v),
        "low": float(low_v),
        "close": float(close_v),
        "volume": float(volume),
        "trades": int(raw[8]) if len(raw) > 8 and raw[8] is not None else None,
        "taker_buy_volume": float(raw[9]) if len(raw) > 9 and raw[9] is not None else None,
        "event_time_utc": ts,
        "received_time_utc": received,
        "source": source,
        "source_id": f"{symbol}:{timeframe}:{ts}",
    }


def _interval_ms(timeframe: str) -> int:
    """Window length of a timeframe. Unknown names raise: a silent default of
    1 minute is how a 4h bar gets mistaken for a closed one."""
    table = {
        "1m": 60_000,
        "5m": 5 * 60_000,
        "15m": 15 * 60_000,
        "30m": 30 * 60_000,
        "1h": 60 * 60_000,
        "4h": 4 * 60 * 60_000,
        "1d": 24 * 60 * 60_000,
    }
    return table[timeframe]


def is_fresh(timeframe: str, received_time_utc: int, now_ms: int | None = None) -> bool:
    now_ms = now_ms or int(time.time() * 1000)
    deadline = FRESHNESS_DEADLINES.get(timeframe, FRESHNESS_DEADLINES["1h"]) * 1000
    return (now_ms - received_time_utc) <= deadline


def detect_gaps(rows: list[dict]) -> list[dict]:
    """Return gap descriptors between successive candles (already sorted by open_time)."""
    gaps = []
    for i in range(1, len(rows)):
        prev = rows[i - 1]
        cur = rows[i]
        expected = prev["open_time_utc"] + _interval_ms(prev["timeframe"])
        if cur["open_time_utc"] != expected:
            gaps.append({"from": prev["open_time_utc"], "to": cur["open_time_utc"], "expected": expected})
    return gaps