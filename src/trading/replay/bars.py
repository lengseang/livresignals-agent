"""Bar: the unit shared by the history loader and the live scanner's bar builder (T3.2)."""
from __future__ import annotations

from dataclasses import dataclass

INTERVAL_MS = {
    "1s": 1_000, "1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    interval: str
    open_ms: int        # milliseconds, always
    end_ms: int         # open_ms + interval: the instant this bar becomes known
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    taker_buy_base: float


@dataclass(frozen=True, slots=True)
class BarOpen:
    """The first print of a bar: everything that is known at its open, and nothing more.

    The live scanner hands this to paper fills at the open instant; the rest of the bar does not exist yet.
    """
    symbol: str
    interval: str
    open_ms: int
    open: float


def to_ms(t: int) -> int:
    """Epoch timestamp in s/ms/µs/ns -> ms, by magnitude.

    Binance spot files switched from ms to µs in 2025, so units are decided per value, never by
    file date. Seconds are refused rather than guessed: no Binance kline file uses them.
    """
    if t >= 10**17:
        return t // 1_000_000        # ns
    if t >= 10**14:
        return t // 1_000            # µs (2025+ spot files: ~1.7e15)
    if t >= 10**11:
        return t                     # ms (2017-2024 files: ~1.5e12)
    raise ValueError(f"timestamp {t} is not ms/µs/ns; refusing to guess its unit")
