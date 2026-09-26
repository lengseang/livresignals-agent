"""Local-vs-exchange clock offset, recorded next to the ticks so replays can correct ts_recv_ns.

The dev PC measured ~5.4 s ahead of Binance on 2026-09-26. Receive stamps are kept raw;
these records let a reader correct them. A failed measurement is UNKNOWN, never 0.
"""
from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable

TIME_URL = "https://data-api.binance.vision/api/v3/time"


def fetch_server_ms() -> int:
    with urllib.request.urlopen(TIME_URL, timeout=10) as r:
        return int(json.load(r)["serverTime"])


def measure(fetch: Callable[[], int] = fetch_server_ms,
            now_ns: Callable[[], int] = time.time_ns) -> dict:
    t0 = now_ns()
    try:
        server_ms = fetch()
    except Exception as e:  # noqa: BLE001 - any failure is recorded, not raised
        return {"kind": "clock", "ts_recv_ns": t0, "offset_ms": None, "status": "UNKNOWN",
                "error": f"{type(e).__name__}: {str(e)[:80]}"}
    t1 = now_ns()
    mid_ms = (t0 + t1) / 2e6
    return {"kind": "clock", "ts_recv_ns": (t0 + t1) // 2, "server_ms": server_ms,
            "offset_ms": server_ms - mid_ms, "rtt_ms": (t1 - t0) / 1e6, "status": "OK"}


class Due:
    """True on the first call and then once per interval (monotonic seconds)."""

    def __init__(self, interval_s: float = 3600.0) -> None:
        self.interval_s = interval_s
        self._last: float | None = None

    def due(self, now_s: float) -> bool:
        if self._last is None or now_s - self._last >= self.interval_s:
            self._last = now_s
            return True
        return False
