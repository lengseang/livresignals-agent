"""a forward lane must not act on a bar that is still forming (2026-10-24).

THE DEFECT THIS PINS
--------------------
`scripts/short_regime_paper.py` was created hours before this test as the forward
record for a hypothesis (regime-gated shorts) that cannot be answered by another
backtest. `bars()` takes the newest rows of the `candles` table; `main()` then
treats the newest row as a CLOSED bar:

    gate_open(con)   -> BTC close vs 200-bar SMA  (entry gate)
    RULES[...](objs) -> entry signal on bar[-1]
    entry            -> bar[-1].close

`scripts/ingest_all.py` runs every 15 minutes at :12/:27/:42/:57 and requests
raw `limit=60` candles *including the in-progress one*, which
`fetch_closed_candles` does not drop, and `upsert_candle` is INSERT OR IGNORE on
(symbol, timeframe, open_time_utc) — the first write of a key wins. So the 4h
row for 12:00 exists from ~12:15 with fifteen minutes of data, and is frozen
there: open/close/high/low/volume never refresh.

Consequences, all silent:
  * a band of 0.04 on the CLOSE — the largest single bar move in the 4h series
    is far above that, so a partial close can flip `close < sma` either way;
  * an entry priced at a close that is not a close, and then settled by the
    stop/target walk over the REAL later bars — the P&L of the trade is measured
    against an entry that never existed;
  * the rules themselves read the partial close (a partial bar can print a
    breakdown or a cross that the completed bar retracts).

The lane's own docstring says fixed in advance so "the forward record cannot be
tuned to the forward sample". A record priced on a forming bar is not tuned —
it is simply not the record it claims to be.

WHAT THE FIX IS
---------------
A bar is closed when `now >= open_time + timeframe`. `last_closed()` filters the
rows the lane already loads, so the gate, the entry and the settle walk all see
the same last closed bar, and no future bar can be read. Nothing here is
specific to 4h: the same predicate guards any timeframe.

These tests are hermetic: a temp SQLite DB shaped like `data/trading.db`, no
network, no live state file.

COMPANION FIX (same class, different file)
------------------------------------------
`tests/test_research_loop_news_cutoff.py` pins the other place the new lane
store hurt the owner's reading of a message: research_loop announced a
"NEW BEST" refusal for a config that had not moved, contradicting the decay
line in the same message.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "src"))

_SPEC = importlib.util.spec_from_file_location(
    "short_regime_paper", _ROOT / "scripts" / "short_regime_paper.py")
lane = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lane)

H = 3600 * 1000
T0 = 1_792_800_000_000                    # a 4h boundary (2026-10-24T00:00Z)


def _db(rows):
    """rows: (symbol, open_time_utc, close, volume) -> in-memory candles DB."""
    con = sqlite3.connect(":memory:")
    con.execute(
        "create table candles (symbol text, timeframe text, open_time_utc integer,"
        " close_time_utc integer, open real, high real, low real, close real,"
        " volume real, primary key (symbol, timeframe, open_time_utc))")
    for sym, ts, close, vol in rows:
        con.execute(
            "insert into candles values (?,?,?,?,?,?,?,?,?)",
            (sym, lane.TF, ts, ts + 4 * H - 1, close, close, close, close, vol))
    return con


def _nice(t):
    """A grid-aligned time > t, at least 5 min later (so `now` is never inside a bar)."""
    day = 24 * H
    return (t // day + 1) * day + 5 * 60 * 1000


def test_last_closed_drops_the_bar_that_is_still_forming():
    # three 4h bars; the newest (opened T0+8h) is still forming at now = T0+9h.
    rows = [("BTC/USDT", T0 + i * 4 * H, 100.0 + i, 1.0) for i in range(3)]
    con = _db(rows)
    rows_ = lane.bars(con, "BTC/USDT", limit=10)
    closed = lane.last_closed(rows_, lane.TF, T0 + 9 * H)
    assert [r[4] for r in closed] == [100.0, 101.0], "forming row must be dropped"


def test_last_closed_keeps_a_bar_whose_window_has_just_ended():
    rows = [("BTC/USDT", T0 + i * 4 * H, 100.0 + i, 1.0) for i in range(3)]
    con = _db(rows)
    rows_ = lane.bars(con, "BTC/USDT", limit=10)
    # exactly at the boundary the third bar (open T0+8h) has closed ...
    assert [r[4] for r in lane.last_closed(rows_, lane.TF, T0 + 12 * H)] == \
        [100.0, 101.0, 102.0]
    # ... and one millisecond earlier it has not
    assert [r[4] for r in lane.last_closed(rows_, lane.TF, T0 + 12 * H - 1)] == \
        [100.0, 101.0]


def _gate_book(partial_close: float, now: int):
    """200 flat BTC bars at 100, then one forming bar whose close is `partial_close`.

    The SMA of the 200 closed bars is 100.0, so `partial_close` alone decides the
    gate — exactly the knife-edge the live lane sits on.
    """
    rows = [("BTC/USDT", T0 - (200 - i) * 4 * H, 100.0, 1.0) for i in range(200)]
    last_open = T0
    rows.append(("BTC/USDT", last_open, partial_close, 1.0))
    assert now < last_open + 4 * H, "the appended bar must be forming"
    return _db(rows)


def test_gate_ignores_a_forming_bar_that_would_flip_it():
    # A forming bar printing 90 (-10%) would open the gate if it were believed.
    con = _gate_book(partial_close=90.0, now=T0 + 1 * H)
    is_open, info = lane.gate_open(con, now=T0 + 1 * H)
    assert is_open is False, "gate opened on a bar that had not closed"
    assert info["sma200"] == 100.0
    assert info["btc_px"] == 100.0, "gate priced the forming bar as the last close"

    # the same bar, once closed, does open the gate
    is_open2, _ = lane.gate_open(con, now=T0 + 4 * H)
    assert is_open2 is True


def test_gate_does_not_reverse_a_real_signal_when_the_forming_bar_is_high():
    # 200 closed bars, all 90, so the last closed close (90) is below the 200-SMA
    # (90.0 exactly is not below it, so the newest closed bar is 89) — and a
    # forming bar at 110 must not shut the gate, because it has not happened.
    rows = [("BTC/USDT", T0 - (200 - i) * 4 * H, 90.0, 1.0) for i in range(200)]
    rows[-1] = ("BTC/USDT", rows[-1][1], 89.0, 1.0)   # newest closed: 89 < 90
    now = _nice(T0 - 1)                                # outside every bar
    rows.append(("BTC/USDT", T0, 110.0, 1.0))          # forming bar, +22%
    con = _db(rows)
    is_open, info = lane.gate_open(con, now=now)
    assert info["btc_px"] == 89.0
    assert is_open is True


def test_entry_bar_is_the_last_closed_bar_not_the_forming_one():
    """A volbreak signal that exists ONLY on the forming bar must not open."""
    # 60 flat bars, then a forming breakdown bar: price under the 20-bar low on
    # 50x volume. If the lane reads it, `f_volbreak(20, 2.0)` fires.
    rows = [("XRP/USDT", T0 - (60 - i) * 4 * H, 1.00, 10.0) for i in range(60)]
    forming = T0
    rows.append(("XRP/USDT", forming, 0.80, 500.0))
    con = _db(rows)

    b = lane.bars(con, "XRP/USDT", limit=260)
    assert lane.RULES["volbreak 20 x2.0"]([lane.Bar(r) for r in b]) is True, (
        "fixture precondition: the raw newest row DOES fire the rule")

    now = forming + 1 * H
    b_closed = lane.last_closed(b, lane.TF, now)
    assert lane.RULES["volbreak 20 x2.0"]([lane.Bar(r) for r in b_closed]) is False
    assert b_closed[-1][4] == 1.00, "entry would be priced off a forming close"


def test_forming_bar_is_dropped_even_when_its_window_has_not_started_yet():
    """The live shape on 2026-10-24: the newest row is the *next* bar.

    The exchange publishes the next candle (open = last close, tiny volume) the
    moment the previous one closes, and ingest writes it within 15 minutes.
    Filtering by "is the newest row newer than its predecessor" would keep it.
    Filtering by "is this bar's own window over" drops it.
    """
    closes = [100.0 + i for i in range(200)]        # 103.0 .. 299.0
    rows = [("BTC/USDT", T0 - (200 - i) * 4 * H, c, 1.0)
            for i, c in enumerate(closes)]
    rows.append(("BTC/USDT", T0, 299.0, 0.02 * 500))  # opened at T0, 15 min in
    con = _db(rows)
    now = T0 + 15 * 60 * 1000
    raw = lane.bars(con, "BTC/USDT", limit=260)
    assert raw[-1][0] == T0, "fixture precondition"
    assert lane.last_closed(raw, lane.TF, now)[-1][0] == T0 - 4 * H
