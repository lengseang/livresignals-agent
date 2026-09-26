"""Core functions of research/08-spread-stats.py, on a tiny hand-made fixture."""
from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "spread_stats", Path(__file__).resolve().parents[2] / "research" / "08-spread-stats.py"
)
ss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ss)

S = 10**9  # one second in ns

# (ts_recv_ns, bid, bid_qty, ask, ask_qty). Hand-computed:
#   holds 1s, 3s, 10s (capped to 5s), and 0 for the last record.
BOOK = [
    (0 * S, 99.995, 1.0, 100.005, 0.01),   # spread 0.01 on mid 100 = 1.0 bp; ask depth $1.00
    (1 * S, 99.99, 2.0, 100.01, 2.0),      # 2.0 bp; depth ~ $200 each side
    (4 * S, 99.995, 1.0, 100.005, 1.0),    # 1.0 bp; depth ~ $100 each side
    (14 * S, 99.95, 1.0, 100.05, 1.0),     # 10.0 bp; last record: weight 0
]


def test_rel_spread_bp():
    assert ss.rel_spread_bp(99.995, 100.005) == pytest.approx(1.0)
    assert ss.rel_spread_bp(99.95, 100.05) == pytest.approx(10.0)


def test_hold_durations_are_capped_and_last_is_zero():
    d = ss.hold_durations_s([r[0] for r in BOOK], max_hold_s=5.0)
    assert d.tolist() == pytest.approx([1.0, 3.0, 5.0, 0.0])


def test_weighted_percentile():
    assert ss.weighted_percentile([1, 2, 3], [1, 1, 1], 0.5) == 2
    assert ss.weighted_percentile([1, 2, 3], [0, 0, 5], 0.5) == 3      # zero weights ignored
    assert ss.weighted_percentile([5, 1], [1, 9], 0.9) == 1            # unsorted input
    assert ss.weighted_percentile([5, 1], [1, 9], 0.95) == 5


def test_book_stats_time_weighted():
    st = ss.book_stats(BOOK, sizes_usd=(5.0, 50.0), max_hold_s=5.0)
    # weights: 1bp for 1s, 2bp for 3s, 1bp for 5s (capped), 10bp for 0s -> 1bp holds 6 of 9 s
    assert st["n"] == 4
    assert st["capped"] == 1
    assert st["hours"] == pytest.approx(9 / 3600)
    assert st["med_bp"] == pytest.approx(1.0)
    assert st["p90_bp"] == pytest.approx(2.0)
    assert st["med_bp_event"] == pytest.approx(1.5)        # plain median of 1, 2, 1, 10
    assert st["share_at_min_spread"] == pytest.approx(6 / 9)
    # a $5 buy exceeds L1 only during the first second (ask depth $1.00)
    assert st["t_exceeds_buy_5"] == pytest.approx(1 / 9)
    assert st["t_exceeds_sell_5"] == pytest.approx(0.0)
    # a $50 buy exceeds L1 in the first second only; $50 fits in $100-$200
    assert st["t_exceeds_buy_50"] == pytest.approx(1 / 9)


def test_locked_book_is_excluded():
    rows = [(0, 100.0, 1.0, 100.0, 1.0), (S, 99.99, 1.0, 100.01, 1.0), (2 * S, 99.99, 1.0, 100.01, 1.0)]
    st = ss.book_stats(rows, max_hold_s=5.0)
    assert st["invalid"] == 1
    assert st["med_bp"] == pytest.approx(2.0)


def test_taker_fit_uses_aggressor_side_and_latest_book():
    # (ts_recv_ns, trade_ms, price, qty, buyer_is_maker)
    trades = [
        (0 * S + 1, 0, 100.005, 0.01, False),   # buy -> ask depth $1.00: $5 exceeds
        (2 * S, 0, 99.99, 1.0, True),           # sell -> bid depth ~$200: fits both
        (20 * S, 0, 99.95, 1.0, False),         # book record 6s old > max age: unmatched
        (-1, 0, 1.0, 1.0, False),               # before any book: unmatched
    ]
    f = ss.taker_fit_at_trades(BOOK, trades, sizes_usd=(5.0, 50.0), max_age_s=5.0)
    assert f["matched"] == 2
    assert f["trade_exceeds_5"] == pytest.approx(0.5)
    assert f["trade_exceeds_50"] == pytest.approx(0.5)


def test_slippage_is_zero_within_l1_and_only_flagged_beyond():
    assert ss.slippage_beyond_best(5.0, 100.0) == 0.0
    assert ss.slippage_beyond_best(50.0, 49.99) == "exceeds top level"


def test_implied_latency_corrects_for_clock_offset():
    # local clock 5000 ms ahead; true latency 300 ms -> raw lag 5300 ms
    trades = [(int((1_000_000 + 5300) * 1e6), 1_000_000, 1.0, 1.0, False)]
    lat = ss.implied_latency_ms(trades, [-5000.0])
    assert lat["lag_raw_ms"] == pytest.approx(5300)
    assert lat["latency_ms"] == pytest.approx(300)


def test_closed_files_skip_the_open_hour(tmp_path):
    d = tmp_path / "2026-01-01" / "book"
    d.mkdir(parents=True)
    rec = {"kind": "book", "symbol": "XUSDT", "ts_exchange_ms": None, "ts_recv_ns": 5,
           "data": {"bid": 1.0, "bid_qty": 2.0, "ask": 1.1, "ask_qty": 3.0, "update_id": 1}}
    with gzip.open(d / "00.jsonl.gz", "wt", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    (d / "01.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")   # still being appended
    files = ss.closed_files(tmp_path, "book")
    assert [p.name for p in files] == ["00.jsonl.gz"]
    book = ss.load_book(files)
    assert book == {"XUSDT": [(5, 1.0, 2.0, 1.1, 3.0)]}
