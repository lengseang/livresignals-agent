"""Combined-stream messages -> typed ticks.

bookTicker payloads carry no timestamp, so ts_exchange_ms stays None for them;
it is never back-filled from the receive time.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Tick:
    kind: str                  # "book" | "trade" | "kline"
    symbol: str
    ts_exchange_ms: int | None
    ts_recv_ns: int
    data: dict

    def to_record(self) -> dict:
        return asdict(self)


def _book(d: dict, ts_recv_ns: int) -> Tick:
    return Tick("book", d["s"], None, ts_recv_ns, {
        "bid": float(d["b"]), "bid_qty": float(d["B"]),
        "ask": float(d["a"]), "ask_qty": float(d["A"]),
        "update_id": int(d["u"]),
    })


def _trade(d: dict, ts_recv_ns: int) -> Tick:
    return Tick("trade", d["s"], int(d["T"]), ts_recv_ns, {
        "price": float(d["p"]), "qty": float(d["q"]),
        "agg_id": int(d["a"]), "first_id": int(d["f"]), "last_id": int(d["l"]),
        "buyer_is_maker": bool(d["m"]), "trade_ms": int(d["T"]),
    })


def _kline(d: dict, ts_recv_ns: int) -> Tick:
    k = d["k"]
    return Tick("kline", d["s"], int(d["E"]), ts_recv_ns, {
        "interval": k["i"], "start_ms": int(k["t"]),
        "open": float(k["o"]), "high": float(k["h"]), "low": float(k["l"]), "close": float(k["c"]),
        "volume": float(k["v"]), "trades": int(k["n"]), "closed": bool(k["x"]),
    })


_BY_SUFFIX = {"bookTicker": _book, "aggTrade": _trade}


def parse(raw: str, ts_recv_ns: int) -> Tick | None:
    """Return a Tick, or None for anything that is not a known market-data message."""
    try:
        msg = json.loads(raw)
        stream, d = msg["stream"], msg["data"]
    except (ValueError, KeyError, TypeError):
        return None
    suffix = stream.split("@", 1)[1] if "@" in stream else ""
    if suffix.startswith("kline_"):
        return _kline(d, ts_recv_ns)
    handler = _BY_SUFFIX.get(suffix)
    return handler(d, ts_recv_ns) if handler else None
