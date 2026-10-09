"""T0.13 — real position drift detector (trend-state.json vs the exchange).

Runs under tests/loop/conftest.py: no network, no real-money import. The
exchange is modelled as a plain {ASSET: qty} dict, which is exactly the
interface `reconcile()` was given so it never touches ccxt in a test.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    """Load a script by path — scripts/ is not a package (see T0.12 tests)."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rpd = _load("real_position_drift")


def pos(symbol, qty, entry=None):
    return {"symbol": symbol, "qty": qty, "entry": entry}


# ---------------------------------------------------------------- classify

def test_match_within_tolerance():
    assert rpd.classify(2.3, 2.3) == "MATCH"
    assert rpd.classify(2.3, 2.29) == "MATCH"      # rounding, not drift


def test_missing_when_exchange_holds_zero():
    assert rpd.classify(34.8, 0.0) == "MISSING"
    assert rpd.classify(0.43, 0.0) == "MISSING"


def test_absent_asset_on_a_successful_read_is_missing():
    # Binance omits zero balances, so absence from a good read means held=0.
    # This is the real 2026-10-09 shape and MUST read as MISSING, not UNKNOWN.
    assert rpd.classify(34.8, None) == "UNKNOWN"   # None = read failed
    rows = rpd.reconcile([pos("ARB/USDT", 34.8)], {"USDT": 20.0})
    assert rows[0]["status"] == "MISSING"


def test_partial():
    assert rpd.classify(58.0, 23.0) == "PARTIAL"   # the 58 -> 23 DOGE shape


def test_excess_when_exchange_holds_more():
    assert rpd.classify(1.0, 1.5) == "EXCESS"


def test_unknown_when_balance_could_not_be_read():
    # None means "we could not read it", never 0 (owner rule 2).
    assert rpd.classify(1.0, None) == "UNKNOWN"


def test_ledger_claims_zero_but_exchange_holds_something():
    assert rpd.classify(0.0, 0.5) == "EXCESS"


# ---------------------------------------------------------------- reconcile

def test_reconcile_reproduces_the_live_2026_10_09_drift():
    """The exact production state that triggered this task."""
    claimed = [pos("ARB/USDT", 34.8), pos("NEAR/USDT", 2.3),
               pos("LINK/USDT", 0.43)]
    exchange = {"ADA": 0.0716, "BNB": 0.00064656, "SUI": 0.1,
                "XRP": 0.0706, "USDT": 20.11}
    rows = rpd.reconcile(claimed, exchange)
    assert [r["status"] for r in rows] == ["MISSING", "MISSING", "MISSING"]
    assert {r["symbol"] for r in rows} == {"ARB/USDT", "NEAR/USDT", "LINK/USDT"}


def test_reconcile_is_worst_first():
    claimed = [pos("A/USDT", 1.0), pos("B/USDT", 1.0), pos("C/USDT", 1.0)]
    exchange = {"A": 1.0, "B": 0.0, "C": 1.5}
    rows = rpd.reconcile(claimed, exchange)
    assert [r["status"] for r in rows] == ["MISSING", "EXCESS", "MATCH"]


def test_reconcile_empty_is_clean():
    assert rpd.reconcile([], {}) == []


def test_base_asset_from_pair_and_bare_symbol():
    assert rpd._base("NEAR/USDT") == "NEAR"
    assert rpd._base("NEARUSDT") == "NEARUSDT"   # venue spelling kept verbatim


# ---------------------------------------------------------------- load

def test_load_real_open_reads_the_nested_path(tmp_path):
    import json
    p = tmp_path / "trend-state.json"
    p.write_text(json.dumps({"real": {"open": [pos("LINK/USDT", 0.43)]}}))
    assert rpd.load_real_open(p) == [pos("LINK/USDT", 0.43)]


def test_load_real_open_raises_on_corrupt_file(tmp_path):
    p = tmp_path / "trend-state.json"
    p.write_text("{not json")
    with pytest.raises(Exception):
        rpd.load_real_open(p)


def test_load_real_open_missing_file_raises(tmp_path):
    with pytest.raises(Exception):
        rpd.load_real_open(tmp_path / "nope.json")


# ---------------------------------------------------------------- render / exit

def test_render_flags_a_wrong_ledger():
    rows = rpd.reconcile([pos("LINK/USDT", 0.43)], {"USDT": 20.0})
    txt = rpd.render(rows)
    assert "MISSING" in txt
    assert "the ledger is wrong" in txt


def test_render_clean_book_makes_no_claim_of_drift():
    rows = rpd.reconcile([pos("ARB/USDT", 34.8)], {"ARB": 34.8})
    assert "0 of 1" in rpd.render(rows)


def test_main_exit_1_on_drift(tmp_path, monkeypatch):
    import json
    p = tmp_path / "trend-state.json"
    p.write_text(json.dumps({"real": {"open": [pos("ARB/USDT", 34.8)]}}))
    monkeypatch.setattr(rpd, "STATE", p)
    monkeypatch.setattr(rpd, "live_balances", lambda: {"USDT": 20.0})
    assert rpd.main([]) == 1


def test_main_exit_0_on_match(tmp_path, monkeypatch):
    import json
    p = tmp_path / "trend-state.json"
    p.write_text(json.dumps({"real": {"open": [pos("ARB/USDT", 34.8)]}}))
    monkeypatch.setattr(rpd, "STATE", p)
    monkeypatch.setattr(rpd, "live_balances", lambda: {"ARB": 34.8})
    assert rpd.main([]) == 0


def test_main_treats_unreadable_exchange_as_unknown_not_missing(tmp_path,
                                                                monkeypatch):
    import json
    p = tmp_path / "trend-state.json"
    p.write_text(json.dumps({"real": {"open": [pos("ARB/USDT", 34.8)]}}))
    monkeypatch.setattr(rpd, "STATE", p)

    def boom():
        raise RuntimeError("no network")

    monkeypatch.setattr(rpd, "live_balances", boom)
    # UNKNOWN is still "not MATCH", so the run fails loudly rather than
    # reporting a clean account it could not actually verify.
    assert rpd.main([]) == 1
