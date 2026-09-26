"""trading.stream.history: public daily files from data.binance.vision, checksum-verified."""
from __future__ import annotations

import hashlib
from datetime import date

import pytest

from trading.stream import history

CHECKSUM_TEXT = "5c62058b85c2c1ff9508fc69f6d7dc5909e18ea5703e830cbbce912e49cbe29f  BTCUSDT-1s-2026-09-24.zip\n"


def test_urls_follow_the_published_layout():
    assert history.file_url("aggTrades", "BTCUSDT", date(2026, 9, 24)) == (
        "https://data.binance.vision/data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-09-24.zip")
    assert history.file_url("klines_1s", "BTCUSDT", date(2026, 9, 24)) == (
        "https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1s/BTCUSDT-1s-2026-09-24.zip")


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        history.file_url("bookTicker", "BTCUSDT", date(2026, 9, 24))   # not published for spot


def test_checksum_file_parsing():
    assert history.parse_checksum(CHECKSUM_TEXT) == "5c62058b85c2c1ff9508fc69f6d7dc5909e18ea5703e830cbbce912e49cbe29f"


def test_verify_accepts_matching_and_rejects_corrupt_bytes(tmp_path):
    p = tmp_path / "f.zip"
    p.write_bytes(b"hello")
    good = hashlib.sha256(b"hello").hexdigest()
    assert history.verify(p, good) is True
    assert history.verify(p, "0" * 64) is False


def test_plan_lists_every_symbol_kind_day_and_skips_verified_files(tmp_path):
    days = [date(2026, 9, 23), date(2026, 9, 24)]
    jobs = history.plan(["BTCUSDT", "ETHUSDT"], ["aggTrades", "klines_1s"], days, tmp_path)
    assert len(jobs) == 8
    done = jobs[0].dest
    done.parent.mkdir(parents=True, exist_ok=True)
    done.write_bytes(b"x")
    (done.parent / (done.name + ".ok")).write_text("verified")
    assert len(history.plan(["BTCUSDT", "ETHUSDT"], ["aggTrades", "klines_1s"], days, tmp_path)) == 7


def test_days_back_ends_yesterday_or_earlier():
    ds = history.days_back(3, today=date(2026, 9, 26))
    assert ds == [date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)]


# --- monthly files (full history) ---------------------------------------------

def test_monthly_urls_follow_the_published_layout():
    assert history.month_url("klines_1m", "BTCUSDT", "2017-08") == (
        "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2017-08.zip")
    assert history.month_url("klines_1s", "SUIUSDT", "2025-09") == (
        "https://data.binance.vision/data/spot/monthly/klines/SUIUSDT/1s/SUIUSDT-1s-2025-09.zip")


def test_months_span_is_inclusive_and_stops_before_the_current_month():
    ms = history.months_span("2025-11", today=date(2026, 2, 14))
    assert ms == ["2025-11", "2025-12", "2026-01"]


def test_months_back_counts_full_months_only():
    assert history.months_back(2, today=date(2026, 9, 26)) == ["2026-07", "2026-08"]


def test_monthly_plan_puts_files_under_monthly_and_skips_verified(tmp_path):
    jobs = history.plan_monthly(["BTCUSDT"], ["klines_1m"], ["2026-07", "2026-08"], tmp_path)
    assert [j.dest.relative_to(tmp_path).as_posix() for j in jobs] == [
        "monthly/klines_1m/BTCUSDT/BTCUSDT-1m-2026-07.zip",
        "monthly/klines_1m/BTCUSDT/BTCUSDT-1m-2026-08.zip",
    ]
