"""Latest top of book and last trade per symbol, for the live scanner."""
from __future__ import annotations

from trading.stream.normalize import Tick


class TopOfBook:
    def __init__(self) -> None:
        self._q: dict[str, dict] = {}

    def update(self, t: Tick) -> None:
        q = self._q.setdefault(t.symbol, {})
        if t.kind == "book":
            q.update(bid=t.data["bid"], ask=t.data["ask"], bid_qty=t.data["bid_qty"],
                     ask_qty=t.data["ask_qty"], quote_recv_ns=t.ts_recv_ns)
        elif t.kind == "trade":
            q.update(last_price=t.data["price"], last_qty=t.data["qty"], trade_ms=t.data["trade_ms"])

    def get(self, symbol: str) -> dict | None:
        q = self._q.get(symbol)
        return dict(q) if q else None
