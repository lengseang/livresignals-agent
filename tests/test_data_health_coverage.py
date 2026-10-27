"""The data-health verdict must be about the STORE, not about the symbols
someone remembered to watch.

Measured 2026-10-24 01:00 UTC. `check_data_health()` said
`ok: true, problems: []` about `/opt/trading-agent/data/trading.db` while:

    5m   ADA ARB AVAX BNB DOGE LINK XRP   21.3h behind  (256 bars, 0 in last 1h)
    15m  all ten symbols                  21.0h behind  (84 bars)
    1h   7 alts                           20.0h behind
    BTC/ETH/SOL 5m                        current

The loop only walks `config.SYMBOLS = [BTC, ETH, SOL]`, so the 7 alts were not
"stale", they were *not looked at*. That is the same defect class as rule #10
(a monitor may not claim health while blind to part of its own instrument) and
#15 (an aggregate must name its cohort): the cohort here is `SYMBOLS`, while
the instrument is the whole table.

Each test below fails against the pre-fix module.
"""
from __future__ import annotations

import time

import pytest

from trading.config import SYMBOLS, TIMEFRAMES
from trading.data.health import check_data_health, series_coverage
from trading.data.store import init, upsert_candle

IV = {"5m": 300_000, "15m": 900_000, "30m": 1800_000, "1h": 3600_000,
      "4h": 14400_000, "1d": 86400_000}


def _candle(symbol, tf, open_ms, close=100.0):
    iv = IV[tf]
    return {"symbol": symbol, "timeframe": tf,
            "open_time_utc": open_ms, "close_time_utc": open_ms + iv - 1,
            "open": close - 1, "high": close + 1, "low": close - 2, "close": close,
            "volume": 1.0, "trades": 1, "taker_buy_volume": 0.5,
            "event_time_utc": open_ms, "received_time_utc": open_ms + iv,
            "source": "test", "source_id": f"{symbol}:{tf}:{open_ms}"}


def _seed_core(con, now_ms):
    """config.SYMBOLS fresh on every timeframe — the state that used to read OK."""
    for sym in SYMBOLS:
        for tf, iv in IV.items():
            upsert_candle(con, _candle(sym, tf, now_ms - 2 * iv))


def _seed_alt(con, now_ms, symbol="ADA/USDT", hours_stale=21):
    """An alt with history back to `hours_stale` and nothing since."""
    for tf, iv in IV.items():
        stop = now_ms - hours_stale * 3600_000
        # dense near the stop, so the series looks alive then dies
        for k in range(14):
            upsert_candle(con, _candle(symbol, tf, stop - k * iv))
    # the newest row must not be a 1d bar: a 1d close can be < 24h old and
    # reorder which symbol defines "the store's newest data" (2026-10-24: the
    # first run of this test had ADA's 1d bar define it, and ADA came out
    # covered because nothing in the store was newer than ADA).
    stop = now_ms - hours_stale * 3600_000
    con.execute("DELETE FROM candles WHERE symbol=? AND timeframe='1d' "
                "AND close_time_utc >= ?", (symbol, stop))
    upsert_candle(con, _candle(symbol, "1d", now_ms - 24 * 3600_000 - 3600_000))


def test_a_store_fresh_only_under_config_symbols_is_not_healthy(tmp_path):
    db = tmp_path / "h.db"
    con = init(db)
    now = int(time.time() * 1000)
    _seed_core(con, now)
    _seed_alt(con, now)
    con.commit()
    con.close()

    rep = check_data_health(db)
    # the candle-only verdict is unchanged — it really is fresh for SYMBOLS
    assert rep["ok"] is True
    # ... but the store is NOT covered, and the report must say so
    assert rep["ok_all"] is False, (
        "7 alts 21h stale and the store reported fully healthy")
    assert rep["coverage"]["ADA/USDT"]["covered"] is False
    assert "behind the store" in rep["coverage"]["ADA/USDT"]["why"]
    assert any("ADA/USDT: not covered" in p for p in rep["problems"])
    # the core symbols must not be swept up in THAT claim: each gets its own
    # stated reason, and whichever fail, fail for a reason other than ADA's
    assert all(rep["coverage"][s]["why"] == "" or "holes" in rep["coverage"][s]["why"]
               for s in SYMBOLS), rep["coverage"]


def test_coverage_counts_bars_not_just_the_latest_close(tmp_path):
    """A series with ONE recent bar and 20h of nothing is not covered.

    The old check could not express this: `MAX(close_time_utc)` is recent, so
    the series is 'fresh', and a replay over the last hour finds one bar.
    """
    db = tmp_path / "h.db"
    con = init(db)
    now = int(time.time() * 1000)
    _seed_core(con, now)
    _seed_alt(con, now - 40 * 60_000)          # history stops 40 min ago
    for tf, iv in IV.items():                   # ... plus one lonely recent bar
        upsert_candle(con, _candle("ADA/USDT", tf, now - 2 * iv))
    con.commit()
    con.close()

    cov = series_coverage(db)
    ada = cov["ADA/USDT"]
    assert ada["bars_win"] < 10, ada
    assert ada["covered"] is False
    # the reason is the hole, not a clock: 11 of 12 slots empty
    # (the message reports the HOLE window, hole_window_h=3, not window_hours=1;
    #  assert on the substring that is actually invariant, not on "1h")
    assert "holes in" in ada["why"], ada["why"]


def test_a_symbol_missing_a_whole_timeframe_is_not_covered(tmp_path):
    """Dense 5m is not coverage for a 4h replay.

    The first plan written on 2026-10-23 said 43.7% of the book had 'no
    candles'. Re-measured today, those symbols DO have 5m rows (written in one
    20-minute backfill window) but no 4h/1d rows — the omission is per-series,
    and a coverage flag that only looks at the newest row cannot see it.
    """
    db = tmp_path / "h.db"
    con = init(db)
    now = int(time.time() * 1000)
    for sym in SYMBOLS:
        for tf, iv in IV.items():
            upsert_candle(con, _candle(sym, tf, now - 2 * iv))
    for k in range(14):                          # NEAR: 5m only
        upsert_candle(con, _candle("NEAR/USDT", "5m", now - k * IV["5m"]))
    con.commit()
    con.close()

    rep = check_data_health(db)
    near = rep["coverage"]["NEAR/USDT"]
    assert near["covered"] is False, near
    assert "series at all" in near["why"], near["why"]
    assert rep["ok_all"] is False


def test_coverage_can_be_switched_off_for_callers_that_only_want_candles(tmp_path):
    db = tmp_path / "h.db"
    con = init(db)
    now = int(time.time() * 1000)
    _seed_core(con, now)
    _seed_alt(con, now)
    con.commit()
    con.close()
    rep = check_data_health(db, coverage=False)
    assert rep["ok"] is True and rep["ok_all"] is True


def test_the_grid_is_anchored_on_closed_bars_not_on_wall_clock(tmp_path):
    """A perfectly dense series must not read as 11 holes.

    First run against the live store, this function anchored its 5m grid on
    `now` instead of on the bar boundary, so every slot sat between two real
    bars: BTC (10 bars in the last hour, contiguous back 20h) was reported
    "11 holes in 1h". Every series was wrong in the same way, which is the
    tell — a check that fails on the healthiest input is measuring itself.
    """
    db = tmp_path / "h.db"
    con = init(db)
    now = int(time.time() * 1000)
    grid_end = (now // IV["5m"]) * IV["5m"] - IV["5m"]
    for sym in SYMBOLS:
        for tf, iv in IV.items():
            upsert_candle(con, _candle(sym, tf, now - 2 * iv))
        for k in range(1, 40):                    # 40 contiguous closed 5m bars
            upsert_candle(con, _candle(sym, "5m", grid_end - k * IV["5m"]))
    con.commit()
    con.close()

    cov = series_coverage(db)
    for sym in SYMBOLS:
        assert cov[sym]["covered"] is True, (sym, cov[sym])
        assert cov[sym]["holes"] == 0, (sym, cov[sym])
        assert cov[sym]["bars_win"] >= 10, (sym, cov[sym])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
