"""Binance PUBLIC market data over REST (read-only, no key, no account, no orders).

Host: data-api.binance.vision, the public market-data mirror (api.binance.com is geo-restricted from some
clouds; this mirror serves klines and book tickers without authentication). Nothing here can place an order:
it only issues GETs to public market-data paths, and the module imports no order client.

  * `klines` returns only CLOSED bars (end_ms <= the server-corrected now): the in-progress bar is dropped.
  * bar `end_ms` is Binance's close_time + 1 (the replay engine's convention): the instant the bar became known.
  * `bookTicker` has no timestamp, so a Book is stamped with the server-corrected receive time.
  * HTTP statuses map to exception CLASSES; a response body is never echoed into an exception.
  * the HTTP layer is injected: tests use a fake and never open a socket.
"""
from __future__ import annotations

import json
import time
from typing import Callable, Optional, Protocol

from trading.replay.bars import Bar
from trading.v2.features import Book

BASE = "https://data-api.binance.vision"
MAX_LIMIT = 1000


class FeedError(Exception):
    pass


class FeedRateLimited(FeedError):       # 429 / 418: back off
    pass


class FeedHttpError(FeedError):
    pass


class FeedMalformed(FeedError):
    pass


class Http(Protocol):
    def get(self, url: str, timeout_s: float) -> tuple[int, bytes]: ...


class UrllibHttp:
    def get(self, url: str, timeout_s: float) -> tuple[int, bytes]:
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout_s) as r:  # noqa: S310
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:                       # DNS / connect / TLS / timeout: class name only
            raise FeedHttpError(type(e).__name__) from None


class BinancePublic:
    def __init__(self, http: Optional[Http] = None, base: str = BASE, timeout_s: float = 10.0,
                 clock: Callable[[], float] = time.time):
        if not base.startswith("https://"):
            raise ValueError("the feed URL must be https")
        self.http, self.base, self.timeout_s, self._clock = http or UrllibHttp(), base, timeout_s, clock
        self.offset_ms = 0                                   # server - local, measured by sync_clock()
        self.last_ok_ms: Optional[int] = None
        self.last_latency_ms: Optional[float] = None
        self.errors = 0

    # ------------------------------------------------------------------ plumbing

    def _get(self, path: str):
        t0 = time.monotonic()
        try:
            status, body = self.http.get(self.base + path, self.timeout_s)
        except FeedError:
            self.errors += 1
            raise
        self.last_latency_ms = (time.monotonic() - t0) * 1000.0
        if status in (418, 429):
            self.errors += 1
            raise FeedRateLimited(str(status))
        if status != 200:
            self.errors += 1
            raise FeedHttpError(str(status))
        try:
            data = json.loads(body)
        except ValueError:
            self.errors += 1
            raise FeedMalformed("not json") from None
        self.last_ok_ms = self.now_ms()
        return data

    def now_ms(self) -> int:
        """Local clock corrected by the measured offset to the exchange's clock."""
        return int(self._clock() * 1000) + self.offset_ms

    # ------------------------------------------------------------------ endpoints

    def server_time_ms(self) -> int:
        d = self._get("/api/v3/time")
        if not isinstance(d, dict) or isinstance(d.get("serverTime"), bool) or not isinstance(d.get("serverTime"), int):
            raise FeedMalformed("no serverTime")
        return d["serverTime"]

    def sync_clock(self) -> int:
        """Measure server - local (midpoint of the request) and remember it. Returns the offset in ms."""
        t0 = self._clock() * 1000
        srv = self.server_time_ms()
        t1 = self._clock() * 1000
        self.offset_ms = int(srv - (t0 + t1) / 2)
        return self.offset_ms

    def klines(self, symbol: str, interval: str, limit: int = 500, end_time_ms: Optional[int] = None,
               now_ms: Optional[int] = None) -> list[Bar]:
        """CLOSED bars, oldest first. `now_ms` defaults to the server-corrected clock."""
        if not (1 <= limit <= MAX_LIMIT):
            raise ValueError(f"limit must be 1..{MAX_LIMIT}")
        q = f"/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        if end_time_ms is not None:
            q += f"&endTime={int(end_time_ms)}"
        rows = self._get(q)
        if not isinstance(rows, list):
            raise FeedMalformed("klines is not a list")
        now = self.now_ms() if now_ms is None else now_ms
        out: list[Bar] = []
        for r in rows:
            try:
                bar = Bar(symbol, interval, int(r[0]), int(r[6]) + 1, float(r[1]), float(r[2]), float(r[3]),
                          float(r[4]), float(r[5]), float(r[7]), int(r[8]), float(r[9]))
            except (IndexError, TypeError, ValueError):
                raise FeedMalformed("bad kline row") from None
            if bar.end_ms <= now:
                out.append(bar)
        return out

    def history(self, symbol: str, interval: str, n: int, now_ms: Optional[int] = None) -> list[Bar]:
        """The last n closed bars, paging backwards if n > 1000."""
        bars: list[Bar] = []
        end = None
        while len(bars) < n:
            # the newest bar of the first page is usually still open and gets dropped: ask for one extra
            want = min(MAX_LIMIT, n - len(bars) + (1 if end is None else 0))
            chunk = self.klines(symbol, interval, want, end_time_ms=end, now_ms=now_ms)
            if not chunk:
                break
            bars = chunk + bars
            end = chunk[0].open_ms - 1
        return bars[-n:]

    def book(self, symbol: str) -> Book:
        sent_ms = self.now_ms()                    # stamped when the request was SENT: the data is at least this fresh
        d = self._get(f"/api/v3/ticker/bookTicker?symbol={symbol}")
        try:
            return Book(ts_ms=sent_ms, bid=float(d["bidPrice"]), ask=float(d["askPrice"]),
                        bid_qty=float(d["bidQty"]), ask_qty=float(d["askQty"]))
        except (KeyError, TypeError, ValueError):
            raise FeedMalformed("bad bookTicker") from None


class LiveBook:
    """A features.BookSource over the live REST book. A failed fetch returns None (the feature engine then
    reports the order book as missing and no snapshot is made: never a stale or invented book)."""

    def __init__(self, feed: BinancePublic):
        self.feed = feed

    def latest(self, symbol: str) -> Optional[Book]:
        try:
            return self.feed.book(symbol)
        except FeedError:
            return None
