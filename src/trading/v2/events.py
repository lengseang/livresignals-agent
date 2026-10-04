"""Contract A producers: closed bars -> MarketEvent. Pure; the caller supplies the receive time."""
from __future__ import annotations

from datetime import datetime, timezone

from trading.replay.bars import Bar
from trading.v2.contracts import MarketEvent
from trading.v2.ids import derive_id


def iso_ms(ms: int) -> str:
    """Epoch milliseconds -> 'YYYY-MM-DDTHH:MM:SS.mmmZ' (UTC)."""
    dt = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms % 1000:03d}Z"


_TF = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}


def kline_closed_event(bar: Bar, received_ms: int, source: str = "ws", venue: str = "binance") -> MarketEvent:
    """A `kline_closed` event for a bar that has CLOSED (it carries end_ms, the instant it became known).
    The id is stable per (venue, symbol, interval, end_ms) so the bus can de-duplicate on replay."""
    if bar.interval not in _TF:
        raise ValueError(f"unsupported interval {bar.interval!r}")
    return MarketEvent(
        contract="market_event", version="1.0",
        event_id=derive_id("mkt", venue, bar.symbol, bar.interval, bar.end_ms),
        symbol=bar.symbol, venue=venue, event_time_utc=iso_ms(bar.end_ms),
        received_time_utc=iso_ms(received_ms), source=source, kind="kline_closed",
        price=bar.close, timeframe=_TF[bar.interval],
    )
