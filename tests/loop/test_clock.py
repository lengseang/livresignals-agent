"""trading.stream.clock: local-vs-exchange clock offset records (T2.2b)."""
from __future__ import annotations

import pytest

from trading.stream import clock


def test_offset_uses_the_request_midpoint():
    # local sent at 1000 ms, received at 1200 ms -> midpoint 1100 ms; server says 1050 ms
    rec = clock.measure(lambda: 1050, now_ns=iter([1_000_000_000, 1_200_000_000]).__next__)
    assert rec["kind"] == "clock"
    assert rec["offset_ms"] == pytest.approx(-50.0)
    assert rec["rtt_ms"] == pytest.approx(200.0)
    assert rec["server_ms"] == 1050


def test_failed_fetch_is_recorded_as_unknown_not_zero():
    def boom():
        raise OSError("timeout")
    rec = clock.measure(boom, now_ns=iter([1, 2]).__next__)
    assert rec["offset_ms"] is None and rec["status"] == "UNKNOWN"
    assert "OSError" in rec["error"]


def test_due_every_interval():
    d = clock.Due(interval_s=3600)
    assert d.due(0.0) is True          # first call always measures
    assert d.due(10.0) is False
    assert d.due(3600.0) is True


def test_server_time_url_is_the_market_data_mirror():
    assert clock.TIME_URL == "https://data-api.binance.vision/api/v3/time"
