"""The public-data feed, on a fake HTTP layer. No network."""
import json

import pytest

from trading.v2.binance_public import (BASE, BinancePublic, FeedHttpError, FeedMalformed, FeedRateLimited, LiveBook,
                                       MAX_LIMIT)

MIN = 60_000


def row(open_ms, o=100.0, h=101.0, l=99.0, c=100.5, v=10.0, qv=1005.0, n=7, tb=4.0, step=MIN):
    # Binance kline: [open_time, open, high, low, close, volume, close_time, quote_vol, trades, taker_buy_base, taker_buy_quote, ignore]
    return [open_ms, str(o), str(h), str(l), str(c), str(v), open_ms + step - 1, str(qv), n, str(tb), "0", "0"]


class Http:
    def __init__(self, routes=None):
        self.routes, self.calls = routes or {}, []

    def get(self, url, timeout_s):
        self.calls.append(url)
        for key, resp in self.routes.items():
            if key in url:
                r = resp(url) if callable(resp) else resp
                return r if isinstance(r, tuple) else (200, json.dumps(r).encode())
        return 404, b"{}"


def feed(routes=None, t=1_700_000_000.0):
    clock = {"t": t}
    f = BinancePublic(Http(routes), clock=lambda: clock["t"])
    f._test_clock = clock
    return f


NOW = 1_700_000_000_000


def test_kline_rows_become_bars_with_end_ms_equal_close_time_plus_one():
    f = feed({"/klines": [row(NOW - 3 * MIN), row(NOW - 2 * MIN, o=100.5, c=101.0)]})
    bars = f.klines("BTCUSDT", "1m", 5, now_ms=NOW)
    assert [b.open_ms for b in bars] == [NOW - 3 * MIN, NOW - 2 * MIN]
    b = bars[0]
    assert (b.end_ms, b.open, b.high, b.low, b.close, b.volume, b.quote_volume, b.trades, b.taker_buy_base) == (
        NOW - 2 * MIN, 100.0, 101.0, 99.0, 100.5, 10.0, 1005.0, 7, 4.0)
    assert b.symbol == "BTCUSDT" and b.interval == "1m"


def test_the_in_progress_bar_is_never_returned():
    f = feed({"/klines": [row(NOW - 2 * MIN), row(NOW - MIN), row(NOW)]})       # the last bar closes in the future
    bars = f.klines("BTCUSDT", "1m", 5, now_ms=NOW)
    assert [b.open_ms for b in bars] == [NOW - 2 * MIN, NOW - MIN] and all(b.end_ms <= NOW for b in bars)
    assert f.klines("BTCUSDT", "1m", 5, now_ms=NOW - 1)[-1].open_ms == NOW - 2 * MIN    # one ms earlier: bar not yet closed


def test_requests_are_public_get_paths_on_the_public_mirror_with_the_right_query():
    f = feed({"/klines": [], "/ticker/bookTicker": {"bidPrice": "1", "askPrice": "1", "bidQty": "1", "askQty": "1"}})
    f.klines("ETHUSDT", "1h", 7, end_time_ms=123, now_ms=NOW)
    f.book("ETHUSDT")
    urls = f.http.calls
    assert urls[0] == f"{BASE}/api/v3/klines?symbol=ETHUSDT&interval=1h&limit=7&endTime=123"
    assert urls[1] == f"{BASE}/api/v3/ticker/bookTicker?symbol=ETHUSDT"
    assert BASE == "https://data-api.binance.vision" and not any("order" in u.lower().replace("bookticker", "") for u in urls)


def test_history_asks_for_one_extra_to_cover_the_open_bar_and_pages_backwards():
    def klines(url):
        end = None if "endTime" not in url else int(url.split("endTime=")[1])
        limit = int(url.split("limit=")[1].split("&")[0])
        last_open = (NOW // MIN) * MIN if end is None else (end // MIN) * MIN
        rows = [row(last_open - i * MIN) for i in range(limit)][::-1]
        return rows
    f = feed({"/klines": klines})
    bars = f.history("BTCUSDT", "1m", 1500, now_ms=NOW)
    assert len(bars) == 1500
    ends = [b.end_ms for b in bars]
    assert all(b - a == MIN for a, b in zip(ends, ends[1:])) and ends[-1] <= NOW
    assert all(int(u.split("limit=")[1].split("&")[0]) <= MAX_LIMIT for u in f.http.calls) and len(f.http.calls) == 2
    calls_before = len(f.http.calls)
    assert len(f.history("BTCUSDT", "1m", 5, now_ms=NOW)) == 5
    assert len(f.http.calls) - calls_before == 1          # the +1 for the open bar means ONE request, not two


def test_history_stops_when_the_exchange_has_no_more():
    f = feed({"/klines": lambda u: [] if "endTime" in u else [row(NOW - 3 * MIN), row(NOW - 2 * MIN)]})
    assert len(f.history("BTCUSDT", "1m", 50, now_ms=NOW)) == 2


def test_book_is_stamped_when_the_request_was_sent_not_when_it_returned():
    f = feed({"/ticker/bookTicker": {"symbol": "X", "bidPrice": "100.5", "askPrice": "100.6", "bidQty": "3", "askQty": "4"}})
    orig = f.http.get

    def slow(url, timeout_s):
        f._test_clock["t"] += 2.0                    # the response takes 2 s
        return orig(url, timeout_s)
    f.http.get = slow
    b = f.book("X")
    assert b.ts_ms == 1_700_000_000_000 and (b.bid, b.ask, b.bid_qty, b.ask_qty) == (100.5, 100.6, 3.0, 4.0)


def test_sync_clock_measures_the_offset_to_the_exchange_midpoint():
    f = feed({"/api/v3/time": {"serverTime": 1_700_000_000_500}})
    assert f.sync_clock() == 500 and f.offset_ms == 500
    assert f.now_ms() == 1_700_000_000_000 + 500


@pytest.mark.parametrize("status,exc", [(429, FeedRateLimited), (418, FeedRateLimited), (500, FeedHttpError),
                                        (403, FeedHttpError), (451, FeedHttpError)])
def test_http_errors_map_to_classes_and_never_echo_the_body(status, exc):
    f = feed({"/klines": (status, b'{"msg": "SECRET-BODY-TEXT"}')})
    with pytest.raises(exc) as e:
        f.klines("BTCUSDT", "1m", 5, now_ms=NOW)
    assert "SECRET" not in str(e.value) and f.errors == 1


@pytest.mark.parametrize("body", [b"<html>", b"", b'{"not": "a list"}', b"null"])
def test_a_non_list_or_non_json_kline_reply_is_malformed(body):
    with pytest.raises(FeedMalformed):
        feed({"/klines": (200, body)}).klines("BTCUSDT", "1m", 5, now_ms=NOW)


@pytest.mark.parametrize("bad", [[[1, 2]], [["x"] * 12], [[NOW, "a", "b", "c", "d", "e", NOW + MIN, "f", 1, "g", "0", "0"]]])
def test_a_bad_kline_row_is_malformed(bad):
    with pytest.raises(FeedMalformed):
        feed({"/klines": bad}).klines("BTCUSDT", "1m", 5, now_ms=NOW)


def test_bad_book_and_time_replies_are_malformed():
    with pytest.raises(FeedMalformed):
        feed({"/bookTicker": {"bidPrice": "x"}}).book("X")
    with pytest.raises(FeedMalformed):
        feed({"/api/v3/time": {"serverTime": True}}).server_time_ms()
    with pytest.raises(FeedMalformed):
        feed({"/api/v3/time": []}).server_time_ms()


def test_limits_and_url_are_validated():
    f = feed()
    for bad in (0, -1, MAX_LIMIT + 1):
        with pytest.raises(ValueError):
            f.klines("BTCUSDT", "1m", bad)
    with pytest.raises(ValueError, match="https"):
        BinancePublic(base="http://data-api.binance.vision")


def test_live_book_returns_none_on_a_failed_fetch_never_an_invented_book():
    ok = feed({"/bookTicker": {"bidPrice": "1", "askPrice": "2", "bidQty": "3", "askQty": "4"}})
    assert LiveBook(ok).latest("X").bid == 1.0
    assert LiveBook(feed({"/bookTicker": (500, b"")})).latest("X") is None
    assert LiveBook(feed({"/bookTicker": (200, b"garbage")})).latest("X") is None


def test_last_ok_and_latency_are_recorded_after_a_good_call_only():
    f = feed({"/api/v3/time": {"serverTime": 1}})
    assert f.last_ok_ms is None and f.last_latency_ms is None
    f.server_time_ms()
    assert f.last_ok_ms == NOW and f.last_latency_ms is not None
