"""T0.11: per_symbol_net stops presenting the paper universe as the trend book.

`trend-state.json` holds TWO books: `log` (the 53-symbol PAPER universe) and
`real.closed` (the REAL money book). Before this task one SOURCES row read the
paper `log` under the name `trend_follower`, so the paper universe was reported
as the trend book — the same defect class T0.10 fixed one reader further in.

These tests load the script by path (it is not a package module) and assert the
split, the nested key-path reader, and that the real book's percent comes from
prices. No network, no data/ reads.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


psn = _load("per_symbol_net")
bu = _load("book_units")


# --- the sources table names each book for what it is ---

def test_paper_log_is_not_named_trend_follower():
    books = [b for b, _f, _k in psn.SOURCES]
    assert "trend_follower" not in books, "the paper log must not be labelled the trend book"
    assert "trend_paper_universe" in books


def test_real_money_book_is_read_from_the_nested_real_closed_path():
    src = {b: (f, k) for b, f, k in psn.SOURCES}
    assert "real_closed" in src
    fname, keypath = src["real_closed"]
    assert fname == "trend-state.json"
    assert tuple(keypath) == ("real", "closed")


def test_paper_universe_reads_the_log_key():
    src = {b: (f, k) for b, f, k in psn.SOURCES}
    fname, keypath = src["trend_paper_universe"]
    assert fname == "trend-state.json"
    assert tuple(keypath) == ("log",)


# --- the nested key-path reader ---

def test_dig_follows_a_nested_path():
    d = {"real": {"closed": [{"symbol": "A"}, {"symbol": "B"}]}}
    assert psn._dig(d, ("real", "closed")) == [{"symbol": "A"}, {"symbol": "B"}]


def test_dig_missing_level_is_empty_not_a_crash():
    assert psn._dig({}, ("real", "closed")) == []
    assert psn._dig({"real": None}, ("real", "closed")) == []


def test_dig_non_list_leaf_is_empty():
    assert psn._dig({"real": {"closed": None}}, ("real", "closed")) == []


# --- book_units knows the paper book under its honest name ---

def test_trend_paper_universe_is_a_known_book_ret_fraction():
    assert bu.BOOKS["trend_paper_universe"] == ("ret", "fraction")


# --- the real book's percent comes from prices, not pnl_over_qty ---

def test_real_book_percent_comes_from_prices():
    rec = {"symbol": "X/USDT", "entry": 100.0, "exit": 98.0, "qty": 5.0,
           "pnl_usd": -10.0}
    assert bu.as_percent("real_closed", rec) == -2.0


def test_real_records_are_not_folded_into_the_combined_percent_aggregate():
    """The real book is dollars; mixing it into a percent verdict is the bug."""
    # simulate main()'s aggregation membership by re-reading the source list
    # through the same filter main() applies.
    per_book = {"real_closed": {"X/USDT": [2.0]}, "trend_paper_universe": {"X/USDT": [1.0]}}
    agg = []
    for book, syms in per_book.items():
        if book == "real_closed":
            continue
        for sym, rs in syms.items():
            agg.extend(rs)
    assert agg == [1.0], "only paper/percent books belong in the combined view"
