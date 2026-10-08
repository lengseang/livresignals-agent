"""T0.10: readers report the REAL money book separately from the paper universe.

Design: docs/loop/designs/T0.5.md §4. The real book's closes live in
`trend-state.json` -> `real.closed` and carry `pnl_usd` (NET of fill fees, or
null/UNKNOWN). The paper log is `trend-state.json` -> `log`. Before this change
three readers presented the paper log as the money book and priced it at a
hardcoded $5 stake; every "real" trend figure downstream was a paper figure.

These tests load the scripts by path (they are not package modules) and assert
the unit/UNKNOWN rules. No network, no data/ reads (tests/loop guards apply).
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


bu = _load("book_units")
fa = _load("fee_audit")


# --- book_units: the real book is D O L L A R S, its percent comes from prices ---

def test_real_closed_is_a_known_book_in_dollars():
    assert bu.BOOKS["real_closed"] == ("pnl_usd", "dollars")


def test_real_closed_percent_comes_from_prices_not_pnl_over_qty():
    # pnl_usd/qty would be a nonsense unit (dollars / base-asset). The percent
    # must be the price move. entry 100 -> exit 102 is +2.00%.
    rec = {"symbol": "X/USDT", "entry": 100.0, "exit": 102.0, "qty": 7.0,
           "pnl_usd": 14.0}
    assert bu.as_percent("real_closed", rec) == 2.0


def test_summarise_dollars_counts_unknown_rather_than_zeroing_it():
    recs = [
        {"symbol": "A/USDT", "pnl_usd": 0.5},
        {"symbol": "B/USDT", "pnl_usd": -0.2},
        {"symbol": "C/USDT", "pnl_usd": None},          # UNKNOWN, must not be 0
    ]
    s = bu.summarise_dollars("real_closed", recs)
    assert s["n_total"] == 3
    assert s["n_known"] == 2
    assert s["n_unknown"] == 1
    assert s["total_usd"] == 0.3                        # not 0.3 + 0 for the null
    assert s["n_wins"] == 1 and s["n_losses"] == 1


def test_summarise_dollars_all_unknown_reports_no_total_not_zero():
    s = bu.summarise_dollars("real_closed", [{"pnl_usd": None}, {"pnl_usd": None}])
    assert s["n_known"] == 0
    assert s["total_usd"] is None                       # never a fabricated 0.00
    assert s["win_rate"] is None


def test_summarise_dollars_empty_book():
    s = bu.summarise_dollars("real_closed", [])
    assert s["n_total"] == 0 and s["total_usd"] is None


def test_summarise_dollars_refuses_a_percent_book():
    try:
        bu.summarise_dollars("regime_trader", [{"ret": 0.01}])
    except ValueError:
        return
    raise AssertionError("percent book must not be read with the dollar summariser")


# --- fee_audit: real and paper are separate blocks, paper is labelled paper ---

def test_fee_audit_separates_real_fills_from_the_paper_log():
    trend = {
        "real": {"closed": [
            {"symbol": "DOGE/USDT", "pnl_usd": 0.0851, "fee_usd": 0.002,
             "entry_status": "BOOKED", "exit_status": "FILL"},
            {"symbol": "X/USDT", "pnl_usd": None, "fee_status": "UNKNOWN"},
        ]},
        "log": [{"ret": 0.01}, {"ret": -0.02}],
    }
    applied = fa.books_from_trend(trend)
    assert "trend_real_closed" in applied
    assert "trend_paper_universe" in applied
    real = applied["trend_real_closed"]
    assert real["trades_priced"] == 1
    assert real["trades_unknown"] == 1
    assert real["net_usd"] == 0.0851                    # the UNKNOWN is not summed in
    assert "paper" in applied["trend_paper_universe"]["note"].lower()


def test_fee_audit_empty_real_book_says_so_instead_of_printing_a_number():
    applied = fa.books_from_trend({"real": {"closed": []}, "log": []})
    real = applied["trend_real_closed"]
    assert real["trades_priced"] == 0
    assert "no real closes" in real["note"]


def test_fee_audit_all_unknown_real_book_still_reports_no_number():
    applied = fa.books_from_trend(
        {"real": {"closed": [{"pnl_usd": None}]}, "log": []})
    real = applied["trend_real_closed"]
    assert real["trades_priced"] == 0
    assert real["trades_unknown"] == 1
    assert "net_usd" not in real                        # no invented total


# --- expectancy_vs_fee: the real section is honest about unknown closes ---

def _expectancy_lines(recs):
    ex = _load("expectancy_vs_fee")
    return ex.real_closed_lines(recs)


def test_expectancy_real_section_empty_book():
    lines = _expectancy_lines([])
    assert any("no real closes recorded yet" in ln for ln in lines)


def test_expectancy_real_section_unknown_only():
    lines = _expectancy_lines([{"pnl_usd": None, "symbol": "X/USDT"}])
    assert any("UNKNOWN" in ln for ln in lines)
    assert not any("USD |" in ln for ln in lines)       # no money figure printed


def test_expectancy_real_section_lists_priced_closes_and_unknown_count():
    lines = _expectancy_lines([
        {"symbol": "DOGE/USDT", "pnl_usd": 0.5, "entry_status": "FILL",
         "exit_status": "FILL"},
        {"symbol": "X/USDT", "pnl_usd": None},
    ])
    joined = "\n".join(lines)
    assert "+0.5000 USD" in joined
    assert "1 of 2 UNKNOWN" in joined
