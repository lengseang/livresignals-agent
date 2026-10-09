"""T0.12: per_symbol_net groups one coin into one row, whatever the spelling.

The books do not agree on the symbol format: trend/unified/adaptive write
`NEAR/USDT`; `regime_trader` trades Bybit linear perps and stores the venue
symbol verbatim, `NEARUSDT` (its `SYMS` list has no slash). Grouping on the raw
key split one coin into two rows in ALL BOOKS COMBINED, halving its n and its
t statistic — the same reader-presents-a-misleading-picture class as T0.10/11.

`norm_symbol()` fixes the FORMAT only. A key whose quote it does not recognise
is returned UNCHANGED, never invented into a slash form.

These tests load the script by path (it is not a package module). No network,
no data/ reads.
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


# --- format, not identity ---

def test_slash_form_is_preserved():
    assert psn.norm_symbol("NEAR/USDT") == "NEAR/USDT"


def test_lowercase_slash_form_is_upper_cased():
    assert psn.norm_symbol("near/usdt") == "NEAR/USDT"


def test_concatenated_venue_symbols_get_a_slash():
    assert psn.norm_symbol("NEARUSDT") == "NEAR/USDT"
    assert psn.norm_symbol("SOLUSDT") == "SOL/USDT"
    assert psn.norm_symbol("XAUTUSDT") == "XAUT/USDT"


def test_usdt_wins_over_usd_when_both_could_match():
    # longest quote first: the base may itself end in "USD".
    assert psn.norm_symbol("XUSDUSDT") == "XUSD/USDT"


def test_unknown_quote_is_left_unchanged_not_invented():
    # No KNOWN quote matches the end -> return as written. Never guess a split.
    assert psn.norm_symbol("NEARBRL") == "NEARBRL"
    assert psn.norm_symbol("NEARJPY") == "NEARJPY"
    # ...but a key ending in a quote we DO know is split there. `TUSD` is not
    # on the list, so `NEARTUSD` ends in `USD` and splits at `USD` — the
    # stated longest-match rule working, not a guess.
    assert psn.norm_symbol("NEARTUSD") == "NEART/USD"


def test_bare_base_with_no_quote_is_unchanged():
    assert psn.norm_symbol("NEAR") == "NEAR"


def test_empty_and_sentinel_are_unchanged():
    assert psn.norm_symbol("") == ""
    assert psn.norm_symbol("?") == "?"


def test_non_string_falls_back_without_crashing():
    assert psn.norm_symbol(None) == "?"


def test_two_distinct_inputs_never_collide():
    # the function only upper-cases and inserts a separator; it drops nothing.
    inputs = ["NEAR/USDT", "NEARUSDT", "neareur", "NEAREUR", "XAUTUSDT", "BTCUSDT"]
    normed = [psn.norm_symbol(x) for x in inputs]
    assert normed.count("NEAR/USDT") == 2      # the two spellings of one coin
    assert len(set(normed)) == len(set(x.upper().replace("/", "") for x in inputs))


# --- the behavioural regression: one coin, one row ---

def _group(symbols, rets):
    """Mimic main()'s grouping: normalise the key, then extend the list."""
    from collections import defaultdict
    per_book = defaultdict(lambda: defaultdict(list))
    for s, r in zip(symbols, rets):
        sym = psn.norm_symbol(s)
        per_book["book"][sym].append(r)
    agg = defaultdict(list)
    for sym, rs in per_book["book"].items():
        agg[sym].extend(rs)
    return agg


def test_one_coin_is_one_row_across_spellings():
    """Before T0.12 this produced 2 rows of n=1; it must be 1 row of n=2."""
    agg = _group(["NEAR/USDT", "NEARUSDT"], [1.0, -0.5])
    assert list(agg.keys()) == ["NEAR/USDT"]
    assert len(agg["NEAR/USDT"]) == 2


def test_distinct_coins_stay_distinct():
    agg = _group(["NEARUSDT", "SOLUSDT"], [1.0, 2.0])
    assert set(agg.keys()) == {"NEAR/USDT", "SOL/USDT"}


# --- the real book's per-symbol grouping also normalises ---

def test_real_book_grouping_normalises_symbols(monkeypatch, capsys):
    recs = [
        {"symbol": "NEARUSDT", "entry": 2.0, "exit": 2.1, "pnl_usd": 0.5},
        {"symbol": "NEAR/USDT", "entry": 2.0, "exit": 1.9, "pnl_usd": -0.2},
    ]
    psn._print_real_book(recs)
    out = capsys.readouterr().out
    assert "NEAR/USDT" in out
    assert "NEARUSDT " not in out      # must not print the raw concatenated key
    assert "n=2" in out                # two records, one row
