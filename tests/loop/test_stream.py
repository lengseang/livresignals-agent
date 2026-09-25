"""trading.stream: normalizer, gap detector, bookTicker throttle, recorder, safety.

Fixtures in fixtures/stream_sample.jsonl are real messages captured from
wss://data-stream.binance.vision on 2026-09-26.
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from trading.stream import ws_client
from trading.stream.book import TopOfBook
from trading.stream.gaps import AggGapDetector
from trading.stream.normalize import Tick, parse
from trading.stream.recorder import Recorder
from trading.stream.throttle import BookThrottle

FIXTURES = Path(__file__).parent / "fixtures" / "stream_sample.jsonl"
RAW = FIXTURES.read_text(encoding="utf-8").splitlines()
HOUR_NS = 3600 * 10**9
T0 = 1_790_300_000 * 10**9          # an arbitrary fixed receive time (ns)


def _ticks(kind):
    return [t for t in (parse(line, T0) for line in RAW) if t and t.kind == kind]


# --- normalize ---------------------------------------------------------------

def test_every_fixture_message_parses():
    ticks = [parse(line, T0) for line in RAW]
    assert all(isinstance(t, Tick) for t in ticks)
    assert {t.kind for t in ticks} == {"book", "trade", "kline"}


def test_book_tick_has_no_exchange_time_and_keeps_receive_time():
    t = _ticks("book")[0]
    assert t.symbol == "BTCUSDT"
    assert t.ts_exchange_ms is None          # bookTicker carries no timestamp
    assert t.ts_recv_ns == T0
    assert t.data["ask"] > t.data["bid"] > 0


def test_trade_tick_fields_are_typed():
    t = _ticks("trade")[0]
    assert isinstance(t.data["agg_id"], int)
    assert isinstance(t.data["price"], float) and isinstance(t.data["qty"], float)
    assert isinstance(t.data["buyer_is_maker"], bool)
    assert t.ts_exchange_ms == t.data["trade_ms"]


def test_kline_tick_fields():
    t = _ticks("kline")[0]
    assert t.data["low"] <= t.data["close"] <= t.data["high"]
    assert isinstance(t.data["closed"], bool)


def test_unknown_or_malformed_messages_are_ignored():
    assert parse('{"result":null,"id":1}', T0) is None
    assert parse("not json", T0) is None


def test_to_record_roundtrips_through_json():
    t = _ticks("trade")[0]
    rec = json.loads(json.dumps(t.to_record()))
    assert rec["kind"] == "trade" and rec["symbol"] == t.symbol and rec["ts_recv_ns"] == T0


# --- gaps --------------------------------------------------------------------

def test_consecutive_agg_ids_report_no_gap():
    g = AggGapDetector()
    assert g.feed("BTCUSDT", 10) is None
    assert g.feed("BTCUSDT", 11) is None


def test_missing_agg_ids_are_reported_as_a_gap():
    g = AggGapDetector()
    g.feed("BTCUSDT", 10)
    gap = g.feed("BTCUSDT", 14)
    assert gap == {"kind": "gap", "symbol": "BTCUSDT", "from_id": 11, "to_id": 13, "missing": 3}


def test_gap_tracking_is_per_symbol():
    g = AggGapDetector()
    g.feed("BTCUSDT", 10)
    assert g.feed("ETHUSDT", 500) is None     # first sight of a symbol is not a gap


def test_real_fixture_trades_have_no_gaps():
    g = AggGapDetector()
    gaps = [g.feed(t.symbol, t.data["agg_id"]) for t in _ticks("trade")]
    assert not any(gaps)


# --- bookTicker throttle -----------------------------------------------------

def _book(bid, ask, ns, bq=1.0, aq=1.0):
    return Tick("book", "BTCUSDT", None, ns, {"bid": bid, "ask": ask, "bid_qty": bq, "ask_qty": aq, "update_id": ns})


def test_throttle_keeps_price_changes_and_drops_qty_only_updates():
    th = BookThrottle(min_interval_ns=10**9)
    assert th.keep(_book(100, 101, 0)) is True           # first
    assert th.keep(_book(100, 101, 1, bq=2.0)) is False  # qty only
    assert th.keep(_book(100, 101.5, 2)) is True         # ask price moved
    assert th.keep(_book(100, 101.5, 3, aq=5.0)) is False
    assert (th.kept, th.dropped) == (2, 2)


def test_throttle_keeps_one_snapshot_per_interval_even_without_price_change():
    th = BookThrottle(min_interval_ns=10**9)
    th.keep(_book(100, 101, 0))
    assert th.keep(_book(100, 101, 10**9 - 1, bq=3.0)) is False
    assert th.keep(_book(100, 101, 10**9, bq=3.0)) is True


# --- recorder ----------------------------------------------------------------

def test_recorder_writes_hourly_files_and_gzips_closed_hours(tmp_path):
    r = Recorder(tmp_path)
    r.write("trade", {"x": 1}, T0)
    r.write("trade", {"x": 2}, T0 + HOUR_NS)        # next hour: previous file is closed
    r.close()
    files = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert all(f.endswith(".jsonl.gz") for f in files) and len(files) == 2
    lines = [json.loads(l) for f in files for l in gzip.open(tmp_path / f, "rt", encoding="utf-8")]
    assert [l["x"] for l in lines] == [1, 2]


def test_recorder_path_uses_utc_date_and_hour(tmp_path):
    r = Recorder(tmp_path)
    r.write("book", {"x": 1}, 0)                    # 1970-01-01 00:00 UTC
    r.close()
    assert (tmp_path / "1970-01-01" / "book" / "00.jsonl.gz").exists()


# --- top of book -------------------------------------------------------------

def test_top_of_book_tracks_latest_quote_and_trade():
    tob = TopOfBook()
    for t in _ticks("book") + _ticks("trade"):
        tob.update(t)
    q = tob.get("BTCUSDT")
    assert q["ask"] > q["bid"] and q["last_price"] > 0
    assert tob.get("NOPE") is None


# --- safety ------------------------------------------------------------------

def test_host_is_pinned_to_the_market_data_only_mirror():
    assert ws_client.HOST == "data-stream.binance.vision"
    url = ws_client.build_url(["btcusdt"], ["bookTicker", "aggTrade"])
    assert url.startswith("wss://data-stream.binance.vision/stream?streams=")
    assert "btcusdt@bookTicker" in url and "btcusdt@aggTrade" in url


def test_default_symbols_are_full_pairs_with_valid_stream_names():
    # Regression: "btc@bookTicker" is silently accepted by the server and sends nothing.
    from trading.stream.run_ingest import KINDS, REAL_SYMS
    assert len(REAL_SYMS) == 15 and all(s.endswith("usdt") for s in REAL_SYMS)
    url = ws_client.build_url(REAL_SYMS, KINDS)
    assert "btcusdt@bookTicker" in url and "@bookTicker/btc@" not in url


def test_stream_package_never_imports_an_exchange_client():
    code = (
        "import sys, trading.stream.run_ingest, trading.stream.ws_client;"
        "bad=[m for m in sys.modules if m=='ccxt' or m.startswith('ccxt.') or m.startswith('trading.exchange')];"
        "print(','.join(bad))"
    )
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
    assert out.stdout.strip() == ""


@pytest.mark.network
@pytest.mark.skipif(os.environ.get("LOOP_NETWORK") != "1", reason="live smoke test; set LOOP_NETWORK=1")
def test_live_smoke_receives_each_stream_type():
    import asyncio
    kinds = asyncio.run(ws_client.sample(["btcusdt"], ["bookTicker", "aggTrade", "kline_1s"], seconds=10))
    assert {"book", "trade", "kline"} <= kinds
