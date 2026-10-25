"""The store must be able to CORRECT a bar it wrote too early (2026-10-25).

THE DEFECT THIS PINS
--------------------
`ingest_all.py` (cron :12/:27/:42/:57) calls `fetch_closed_candles`, which is a
thin wrapper over `ex.fetch_ohlcv` — the exchange's raw array, INCLUDING the
in-progress candle. `normalize_candle` does drop a forming bar, but only when
the fetcher's clock sees `now < close_time`; the write path is
`INSERT OR IGNORE` on (symbol, timeframe, open_time_utc), so whatever row is
written FIRST for a key wins forever.

Those two are not the same rule, and the ledger shows the gap. Scanned over
`data/trading.db` on 2026-10-25 01:00 UTC, rows whose `received_time_utc` was
recorded less than 10% into their own window — i.e. a bar captured at its very
first block — still exist for every timeframe:

    timeframe   rows captured <10% into window
    15m          210
    30m          210
    1d            10   (BTC 10-04, received 92.8 min into a 1440 min window)
    4h            10
    1h            10
    5m            63

The 2026-10-24 pass fixed this at the READERS (`short_regime_paper.last_closed`,
`scrape_history.rows_for_batch`). That is necessary and it is not sufficient: a
frozen bar stays in the table, so every reader must re-know the rule, and any
reader that forgets is silently priced on a partial candle. The store is the
one place that can settle it.

WHAT THE FIX IS
---------------
`upsert_candle` keeps INSERT OR IGNORE for a row that is still FORMING — a
partial bar must never be persisted at all, and if one already was, an ingest
that sees it again while forming must not overwrite it with other partial data.

A row that has ENDED (`now >= open_time + timeframe`) is final, and final data
must beat whatever is stored: `ON CONFLICT ... DO UPDATE` replaces it when the
stored row was captured earlier in its window (a frozen partial bar and every
bar written before the exchange published its final volume). A row whose stored
copy was already complete is left alone — re-ingesting a closed bar must not
rewrite identical numbers, because `received_time_utc` is evidence of when the
system first saw the bar.

These tests are hermetic: a temp SQLite DB from the real schema, no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from trading.data import store  # noqa: E402

H = 3_600_000
OPEN_MS = 1_792_800_000_000          # an arbitrary aligned 1h open


def _row(**kw) -> dict:
    base = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "open_time_utc": OPEN_MS,
        "close_time_utc": OPEN_MS + H - 1,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
        "trades": 5,
        "taker_buy_volume": 4.0,
        "event_time_utc": OPEN_MS + H,
        "received_time_utc": OPEN_MS + H,      # seen after the bar closed
        "source": "binance.vision",
        "source_id": f"BTC/USDT:1h:{OPEN_MS}",
    }
    base.update(kw)
    return base


def _stored(con):
    cur = con.execute(
        "SELECT close, high, low, volume, received_time_utc FROM candles "
        "WHERE symbol='BTC/USDT' AND timeframe='1h' AND open_time_utc=?",
        (OPEN_MS,),
    )
    return cur.fetchone(), cur.rowcount


def _db(tmp_path):
    return store.init(tmp_path / "t.db")


def test_a_forming_bar_is_never_persisted(tmp_path):
    """A bar whose window has not ended must leave the store empty."""
    con = _db(tmp_path)
    partial = _row(close=100.05, high=100.1, low=100.0, volume=0.4,
                   received_time_utc=OPEN_MS + 5 * 60_000,
                   event_time_utc=OPEN_MS + 5 * 60_000)
    assert store.upsert_candle(con, partial, now_ms=OPEN_MS + 60_000) is False
    assert _stored(con)[0] is None


def test_a_frozen_partial_bar_is_corrected_by_the_final_row(tmp_path):
    """The 15m/1d case: first write wins today; the final close must win."""
    con = _db(tmp_path)
    # written 0.5 min into a 15m bar and frozen there (the real ledger shape)
    frozen = _row(timeframe="15m", close_time_utc=OPEN_MS + 900_000 - 1,
                  close=100.05, high=100.1, low=100.0, volume=0.4,
                  received_time_utc=OPEN_MS + 30_000,
                  event_time_utc=OPEN_MS + 900_000,
                  source_id=f"BTC/USDT:15m:{OPEN_MS}")
    # allowed through while forming? No — the store refuses it outright now.
    assert store.upsert_candle(con, frozen, now_ms=OPEN_MS + 30_000) is False
    final = _row(timeframe="15m", close_time_utc=OPEN_MS + 900_000 - 1,
                 close=98.0, high=100.1, low=97.0, volume=88.0,
                 received_time_utc=OPEN_MS + 900_000 + 1_000,
                 event_time_utc=OPEN_MS + 900_000,
                 source_id=f"BTC/USDT:15m:{OPEN_MS}")
    assert store.upsert_candle(con, final, now_ms=OPEN_MS + 900_000 + 1_000) is True
    got = con.execute(
        "SELECT close, high, low, volume FROM candles WHERE timeframe='15m'"
    ).fetchone()
    assert tuple(got) == (98.0, 100.1, 97.0, 88.0)


def test_a_legacy_partial_row_is_repaired_in_place(tmp_path):
    """A row already frozen in the table (any source) is corrected, not shadowed."""
    con = _db(tmp_path)
    # simulate the pre-fix writer: a partial 4h row that got in anyway
    con.execute(
        "INSERT INTO candles(symbol,timeframe,open_time_utc,close_time_utc,open,"
        "high,low,close,volume,event_time_utc,received_time_utc,source,source_id)"
        " VALUES('BTC/USDT','4h',?,?,?,?,?,?,?,?,?,'backfill','x')",
        (OPEN_MS, OPEN_MS + 4 * H - 1, 100.0, 100.2, 99.9, 100.1, 3.0,
         OPEN_MS + 4 * H, OPEN_MS + 5 * 60_000),
    )
    con.commit()
    final = _row(timeframe="4h", close_time_utc=OPEN_MS + 4 * H - 1,
                 close=97.5, high=100.9, low=96.0, volume=250.0,
                 received_time_utc=OPEN_MS + 4 * H + 2_000,
                 event_time_utc=OPEN_MS + 4 * H,
                 source_id=f"BTC/USDT:4h:{OPEN_MS}")
    store.upsert_candle(con, final, now_ms=OPEN_MS + 4 * H + 2_000)
    got = con.execute(
        "SELECT close, high, low, volume, received_time_utc FROM candles "
        "WHERE timeframe='4h'"
    ).fetchone()
    assert tuple(got) == (97.5, 100.9, 96.0, 250.0, OPEN_MS + 4 * H + 2_000)


def test_a_closed_bar_is_not_rewritten_by_a_second_complete_ingest(tmp_path):
    """Idempotency: complete data is left alone, received_time is not advanced."""
    con = _db(tmp_path)
    first = _row(received_time_utc=OPEN_MS + H + 15 * 60_000)
    assert store.upsert_candle(con, first, now_ms=OPEN_MS + H + 15 * 60_000) is True
    again = _row(received_time_utc=OPEN_MS + H + 30 * 60_000)
    assert store.upsert_candle(con, again, now_ms=OPEN_MS + H + 30 * 60_000) is False
    assert _stored(con)[0][4] == OPEN_MS + H + 15 * 60_000
    n = con.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
    assert n == 1


def test_a_bar_is_judged_closed_by_the_clock_given_to_the_writer(tmp_path):
    """Closed-ness is decided by `now_ms`, so a caller can be tested at a seam."""
    con = _db(tmp_path)
    snapshot = _row(close=100.05, volume=0.1,
                    received_time_utc=OPEN_MS + 60_000)
    # the caller's clock is still inside the window: nothing is persisted
    assert store.upsert_candle(con, snapshot, now_ms=OPEN_MS + 60_000) is False
    assert _stored(con)[0] is None
    # the same fetch, replayed after the bar ended: now it is final data
    assert store.upsert_candle(con, snapshot, now_ms=OPEN_MS + H + 1) is True
    assert _stored(con)[0][0] == 100.05
