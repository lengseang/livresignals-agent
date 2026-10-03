"""Unit tests for trading.signals.okx_oi — pure logic, no network.

HTTP is never called: to_daily_series / oi_change_pct / parses are pure, and
fetch_page is exercised with a monkeypatched _post.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from trading.signals import okx_oi  # noqa: E402


def _row(y, m, d, oi_usd):
    ts = int(dt.datetime(y, m, d, tzinfo=dt.timezone.utc).timestamp() * 1000)
    return {"ts": ts, "oiUsd": oi_usd, "oiCcy": 1.0, "oiCont": 1.0,
            "oiDeltaUsd": 0.0, "oiDeltaPct": 0.0}


def test_to_daily_series_sorts_and_dedupes():
    rows = [_row(2024, 1, 3, 300), _row(2024, 1, 1, 100),
            _row(2024, 1, 2, 200), _row(2024, 1, 2, 250)]
    dates, oi = okx_oi.to_daily_series(rows)
    assert dates == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert oi == [100.0, 250.0, 300.0]           # last write wins on dup date


def test_oi_change_pct_basic():
    oi = [100.0, 110.0, 121.0, 133.1]
    assert abs(okx_oi.oi_change_pct(oi, 3, 3) - 33.1) < 0.01


def test_oi_change_pct_insufficient_history():
    oi = [100.0, 110.0]
    assert okx_oi.oi_change_pct(oi, 1, 3) is None       # idx-3 < 0
    assert okx_oi.oi_change_pct(oi, 5, 3) is None       # idx out of range


def test_oi_change_pct_zero_base():
    oi = [0.0, 10.0, 20.0, 30.0]
    assert okx_oi.oi_change_pct(oi, 3, 3) is None


def test_fetch_page_rejects_bad_bar():
    try:
        okx_oi.fetch_page("BTC-USDT-SWAP", bar="7m")
        assert False, "should have raised"
    except ValueError:
        pass


def test_fetch_page_parses_rows(monkeypatch=None):
    class _Fake:
        def read(self):
            import json
            return json.dumps({"code": "0", "data": [
                {"rows": [_row(2024, 1, 1, 100)]}]}).encode()

    orig = okx_oi._post
    okx_oi._post = lambda body, timeout=25: __import__("json").loads(_Fake().read())
    try:
        rows = okx_oi.fetch_page("BTC-USDT-SWAP", bar="1D", limit=500)
        assert len(rows) == 1 and rows[0]["oiUsd"] == 100
    finally:
        okx_oi._post = orig


def test_fetch_page_empty_data():
    orig = okx_oi._post
    okx_oi._post = lambda body, timeout=25: {"code": "0", "data": []}
    try:
        assert okx_oi.fetch_page("XXX-USDT-SWAP") == []
    finally:
        okx_oi._post = orig
