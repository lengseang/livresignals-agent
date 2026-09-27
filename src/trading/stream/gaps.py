"""aggTrade ids are consecutive per symbol; a jump means trades we never saw."""
from __future__ import annotations


class AggGapDetector:
    def __init__(self) -> None:
        self._last: dict[str, int] = {}

    def feed(self, symbol: str, agg_id: int) -> dict | None:
        prev = self._last.get(symbol)
        self._last[symbol] = agg_id
        if prev is None or agg_id <= prev + 1:
            return None
        return {"kind": "gap", "symbol": symbol, "from_id": prev + 1, "to_id": agg_id - 1,
                "missing": agg_id - prev - 1}

    def reset(self) -> None:
        """After a reconnect the next id is not comparable; the reconnect itself is logged as a gap."""
        self._last.clear()
