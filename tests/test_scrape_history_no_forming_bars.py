"""The backfiller must never persist a bar whose window has not ended (2026-10-24).

THE DEFECT THIS PINS
--------------------
`scripts/scrape_history.py` is the ONLY writer of `source='backfill'` rows, and
it wrote the in-progress candle on every page: Binance's page includes it, and
the loop converted every row to a candle without asking whether the bar was
finished. The write is `INSERT OR IGNORE` on
(symbol, timeframe, open_time_utc) — the first write of a key wins forever — so a
forming bar stored once is frozen at that partial OHLCV. No later ingest can
correct it, because `normalize_candle` correctly drops forming bars and therefore
never produces a row for that key to overwrite with.

Measured 2026-10-24 18:00 UTC, all 10 tracked symbols:

    BTC/USDT 1d 2026-10-24  close 80966.00  vol  1031   (live 80294.54, vol 10122)
    ETH/USDT 1d 2026-10-24  close  2385.42              (live  2323.07)  +2.68%
    SOL/USDT 1d 2026-10-24  close   102.73              (live   100.54)  +2.18%

i.e. the daily bar was the morning's snapshot presented as the day's close: 0.8-2.7%
wrong on price and ~10x understated on volume, for the whole trading day.

The consumer that turns this into money is `trading.allocation.forward.status()`,
which prices a portfolio off `SELECT close ... timeframe='1d' ORDER BY
open_time_utc DESC LIMIT 1` — the newest row, which was this one.

WHAT THE FIX IS
---------------
`rows_for_batch()` drops any bar whose window has not ended, so a forming bar is
never offered to the writer at all. Guarding at the writer is the only placement
that cannot be bypassed by a future consumer forgetting to filter on read.

The control test matters: a filter that drops EVERYTHING also stops the bug, and
would silently starve the store. Both directions are asserted.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scrape_history import rows_for_batch  # noqa: E402

DAY = 86_400_000


def _bar(open_ms: int, close: float = 100.0) -> list:
    return [open_ms, 99.0, 101.0, 98.0, close, 5000.0]


def test_a_forming_bar_is_never_persisted():
    """The predicate: a bar opened at 00:00, read at 18:00, has 6 hours to run."""
    now = 1_000 * DAY + 18 * 3_600_000          # 18:00 into the bar's own day
    today = 1_000 * DAY                          # bar opened at 00:00
    rows = rows_for_batch([_bar(today)], "BTC/USDT", "1d", DAY, now)
    assert rows == [], "a bar with 6 hours left to run was about to be stored"


def test_a_closed_bar_is_persisted_unchanged():
    """CONTROL — the guard must not starve the store. A closed bar passes whole."""
    now = 1_000 * DAY + 18 * 3_600_000
    yesterday = 999 * DAY
    rows = rows_for_batch([_bar(yesterday, close=123.45)], "BTC/USDT", "1d", DAY, now)
    assert len(rows) == 1
    sym, tf, open_ms, close_ms, o, h, l, c, v = rows[0][:9]
    assert (sym, tf, open_ms) == ("BTC/USDT", "1d", yesterday)
    assert close_ms == yesterday + DAY - 1        # last ms of the bar's window
    assert c == 123.45


def test_the_boundary_is_the_open_plus_span_not_the_close_time():
    """At exactly open+span the bar is done; one millisecond earlier it is not.

    This is the off-by-one that decides whether the row can still change.
    """
    open_ms = 1_000 * DAY
    just_before = open_ms + DAY - 1
    exactly = open_ms + DAY
    assert rows_for_batch([_bar(open_ms)], "S", "1d", DAY, just_before) == []
    assert len(rows_for_batch([_bar(open_ms)], "S", "1d", DAY, exactly)) == 1


def test_a_page_of_history_keeps_every_closed_bar_and_drops_only_the_forming_one():
    """The realistic shape: 60 bars back plus the in-progress one on the end."""
    span = 4 * 3_600_000
    now = 10_000 * DAY + 18 * 3_600_000
    last_closed = (now // span) * span - span
    batch = [_bar(last_closed - k * span) for k in range(59, -1, -1)]
    batch.append(_bar(last_closed + span))              # the forming bar
    rows = rows_for_batch(batch, "ETH/USDT", "4h", span, now)
    assert len(rows) == 60
    assert max(r[2] for r in rows) == last_closed
    assert all(r[3] <= now for r in rows), "a stored row ended in the future"
