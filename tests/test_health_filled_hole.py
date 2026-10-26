"""A 5m hole that a later write FILLED must not be reported as a live fault.

Measured 2026-10-25 18:05 UTC, right after `scripts/ingest_alts.py` backfilled
7 alts: `series_coverage` reported

    BTC/USDT  '1 holes in 3h (first 15:00Z), 0.1h behind the store'

while the 15:00Z bar was present in the table (verified by direct SELECT) and
BTC's newest bar was 5 minutes old. The verdict came from the 3-hour hole
window being re-derived per call; `store.upsert_candle`'s dedupe path settles a
late closed bar over a partial one, so a hole seen at 18:00 can legitimately be
gone by 18:05 — a monitor must re-read before calling it a fault, and must
still call a REAL hole a fault.

Both directions are asserted: `test_a_filled_hole_is_not_a_fault` and
`test_a_real_hole_is_still_a_fault`. A check that can only pass is not a check.
"""
from __future__ import annotations

import time

import pytest

from trading.data.health import check_data_health, series_coverage
from trading.data.normalize import normalize_candle
from trading.data.store import init, upsert_candle

IV = {"5m": 300_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
CORE = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]


def _candle(symbol, tf, open_ms, close=100.0):
    iv = IV[tf]
    raw = [open_ms, close - 1, close + 1, close - 2, close,
           1.0, open_ms + iv - 1, 1.0, 1, 0.5, 0.5, 0]
    row = normalize_candle(raw, symbol, tf, source="test", now_ms=open_ms + iv)
    assert row and row["open_time_utc"] == open_ms
    return row


def _seed(con, sym, tf, start_ms, n, skip_opens=()):
    iv = IV[tf]
    for k in range(n):
        o = start_ms + k * iv
        if o in skip_opens:
            continue
        row = _candle(sym, tf, o)
        upsert_candle(con, row, now_ms=row["close_time_utc"] + 1)
    con.commit()


def _series_coverage(db, **kw):
    """`series_coverage` on a `tmp_path` DB.

    Measured the hard way: passing a `PosixPath` to `series_coverage` falls
    through `db_path or SQLITE_PATH` and then `f"file:{db_path}?mode=ro"`, which
    opens the **LIVE STORE** read-only. Every fixture assertion then compared a
    live verdict against a temp fixture and failed for a reason that had nothing
    to do with the fixture. Pass `str()` — and keep this helper as the one call
    site so the trap is not repeated.
    """
    return series_coverage(str(db), **kw)


def _dense_now(con, now_ms, skip_opens=(), seed_5m=True):
    """CORE symbols dense on every tf, each series ending at its own newest
    CLOSED bar, with the slots in `skip_opens` genuinely absent.

    Every constant here was measured, not reasoned, and the three fixture bugs
    this replaced each made the test pass or fail for a reason unrelated to the
    code under test:

    1. `6h // 1d = 0` → 1d got zero bars and the verdict became
       `no 1d series at all`.
    2. A hole removed from 5m only is CORRECTLY not a fault: `series_coverage`
       exempts a symbol when `lag_h <= grace_h`, and `lag_h` comes from
       MAX(close) over ALL timeframes — so a holey 5m beside a current 1h is
       `covered: true, holes: 2`. A real fault means the symbol is behind on
       every timeframe, which is the live shape measured on 2026-10-25.
    3. `covered` is ALSO true whenever `lag_h <= grace_h` (grace defaults to
       0 and `lag_h` is 0 for any symbol that has data in the store's newest
       timeframe), so `covered` cannot distinguish a hole from a healthy
       series in a fixture. A 5m hole is read off `why`/`holes`, not off
       `covered` — see `_hole_line()` below. This is a real weakness of the
       flag, worth knowing before anyone gates on it.
    4. Seeding a series through its CURRENT, part-open slot gives it a close
       in the FUTURE (1h opened 18:00, close 18:59, at wall-clock 18:05) which
       then becomes `store_latest` and exempts everything. Each series now ends
       at `(now // iv) * iv - iv`: the newest bar that has actually closed.
    """
    for sym in CORE:
        for tf, iv in IV.items():
            if tf == "5m" and not seed_5m:
                continue
            n = int(6 * 3600_000 // iv)
            if n == 0:
                n = 8                                   # 1d: give it history
            newest_closed = (now_ms // iv) * iv - iv
            start = newest_closed - (n - 1) * iv
            skip = skip_opens if tf in ("5m", "30m", "1h") else ()
            _seed(con, sym, tf, start, n, skip_opens=skip)


def test_a_filled_hole_is_not_a_fault(tmp_path):
    """The bar is in the store: no hole may be reported.

    `covered` is not the assertion — see trap 3 in `_dense_now`. The verdict
    under test is the `why` line `check_data_health` turns into a problem.
    """
    db = tmp_path / "h.db"
    con = init(db)
    now = int(time.time() * 1000)
    grid = (now // IV["5m"]) * IV["5m"] - IV["5m"]
    hole = grid - 2 * 12 * IV["5m"]            # 2h back, inside the 3h window
    _dense_now(con, now)
    assert con.execute("SELECT COUNT(*) FROM candles WHERE timeframe='5m' "
                       "AND open_time_utc=?", (hole,)).fetchone()[0] == 3
    why = _series_coverage(db)["BTC/USDT"]["why"]
    con.close()
    assert "holes in" not in why, why
    rep = check_data_health(db, coverage=True)
    assert [p for p in rep["problems"] if "holes in" in p] == []


def test_a_real_hole_is_still_a_fault(tmp_path):
    """The slot is genuinely absent on every sub-hour series → must be reported."""
    db = tmp_path / "h.db"
    con = init(db)
    now = int(time.time() * 1000)
    grid = (now // IV["5m"]) * IV["5m"] - IV["5m"]
    slot = grid - 2 * 12 * IV["5m"]
    holes = {(slot // IV[tf]) * IV[tf] for tf in ("5m", "30m", "1h")}
    _dense_now(con, now, skip_opens=holes)
    for tf in ("5m", "30m", "1h"):
        o = (slot // IV[tf]) * IV[tf]
        assert con.execute("SELECT COUNT(*) FROM candles WHERE timeframe=? "
                           "AND open_time_utc=?", (tf, o)).fetchone()[0] == 0
    con.close()

    # The full window (1h) still has 12 bars, so the count is healthy; only a
    # longer window shows the hole — `hole_window_h=3` is the default and the
    # 2h-old slot is inside it.
    c = _series_coverage(db, window_hours=6)["BTC/USDT"]
    assert "holes in" in c["why"], c
    rep = check_data_health(db, coverage=True)
    assert [p for p in rep["problems"] if "holes in" in p], \
        "a real 3h-window hole was silently cleared"


def test_a_dark_series_is_not_hidden_by_the_other_timeframes(tmp_path):
    """The live shape of 2026-10-25: the 5m series dies, the 1h series keeps
    landing, `lag_h` stays 0 and `covered` stays True. The store is NOT fine —
    every 5m replay that night scored the wrong bars — so a report that only
    says `covered: true` is the outage reporting as health."""
    db = tmp_path / "h.db"
    con = init(db)
    now = int(time.time() * 1000)
    _dense_now(con, now, seed_5m=False)
    # 5m for BTC exists but its newest bar is 6h old. `_seed` walks FORWARD
    # over `n` bars, so n must be 12 here, not the 12 used above to reach the
    # present — seen twice now, from the same assumption.
    stop = (now // IV["5m"]) * IV["5m"] - 6 * 3600_000
    _seed(con, "BTC/USDT", "5m", stop - 11 * IV["5m"], 12)
    assert con.execute("SELECT MAX(open_time_utc) FROM candles WHERE "
                       "symbol='BTC/USDT' AND timeframe='5m'").fetchone()[0] < stop
    con.close()
    c = _series_coverage(db)["BTC/USDT"]
    assert c["covered"] is True        # the flag CANNOT see this — documented
    assert c["bars_win"] < 12          # but the count does
