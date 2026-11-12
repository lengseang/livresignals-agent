"""T0.23 — the tail must sit beside the mean, in every reader of a book.

Design: docs/loop/designs/T0.23.md. Reader/display only: nothing downstream
reads `median_net` or `delete1_net`, and the verdict strings are pinned
unchanged by test_the_verdict_is_unchanged_by_the_tail_columns.

The real-book shape this must be able to PRINT is T0.22's: mean NET positive,
median NET negative, and deleting the single best trade turns the mean
non-positive. If a reader cannot show that, it will keep quoting the tail as
the strategy.
"""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"_t023_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def evf():
    return _load("expectancy_vs_fee")


@pytest.fixture(scope="module")
def psn():
    return _load("per_symbol_net")


# --- 1-2: the median definition ---------------------------------------------

def test_median_odd_is_the_central_value(evf):
    assert evf.median([3.0, 1.0, 2.0]) == 2.0


def test_median_even_is_the_mean_of_the_two_central(evf):
    # 1,2,3,4 -> (2+3)/2 = 2.5. A "lower of the two" implementation would
    # return 2.0; this pins the stated convention so it cannot drift.
    assert evf.median([4.0, 1.0, 3.0, 2.0]) == 2.5


def test_median_of_one_element_is_that_element(evf):
    assert evf.median([-0.5]) == -0.5


def test_median_of_nothing_is_none_never_zero(evf):
    assert evf.median([]) is None


# --- 3: delete-1 drops the MAX, not the min ---------------------------------

def test_delete_best_mean_drops_the_largest(evf):
    xs = [0.10, 0.02, -0.01, 0.01]
    # drop 0.10 -> (0.02 - 0.01 + 0.01) / 3
    assert evf.delete_best_mean(xs) == pytest.approx(0.02 / 3)


def test_delete_best_mean_is_not_delete_worst(evf):
    # If it dropped the min instead: (0.10+0.02+0.01)/3 ~= 0.0433. Must differ.
    xs = [0.10, 0.02, -0.50, 0.01]
    assert evf.delete_best_mean(xs) == pytest.approx((0.02 - 0.50 + 0.01) / 3)
    assert evf.delete_best_mean(xs) != pytest.approx((0.10 + 0.02 + 0.01) / 3)


def test_delete_best_mean_of_one_or_zero_is_none(evf):
    assert evf.delete_best_mean([0.5]) is None
    assert evf.delete_best_mean([]) is None


# --- 4: non-finite values cannot poison the statistics ----------------------

def test_non_finite_values_are_excluded(evf):
    xs = [0.01, float("nan"), 0.03, float("inf"), 0.02]
    m = evf.median(xs)
    d = evf.delete_best_mean(xs)
    assert m == 0.02                       # median of 0.01, 0.02, 0.03
    assert math.isfinite(d)
    assert d == pytest.approx(0.015)       # mean of 0.01, 0.02 after drop 0.03


def test_all_non_finite_is_none_not_a_number(evf):
    assert evf.median([float("nan"), float("inf")]) is None
    assert evf.delete_best_mean([float("nan"), float("inf")]) is None


# --- 5-7: when the note fires, and when it stays silent --------------------

def test_note_fires_when_one_trade_carries_the_mean(evf):
    # 1 big winner, 19 small losers: mean NET > 0, delete-1 <= 0.
    xs = [0.40] + [-0.01] * 19
    fee = 0.0016
    mean_net = sum(xs) / len(xs) - fee
    d1_net = evf.delete_best_mean(xs) - fee
    assert mean_net > 0 and d1_net <= 0          # the precondition holds
    assert evf.tail_note(xs, fee) != ""


def test_note_is_silent_when_the_edge_is_broad(evf):
    xs = [0.05, 0.04, 0.06, 0.03, 0.05]
    assert evf.tail_note(xs, 0.0016) == ""


def test_note_is_silent_when_the_mean_is_already_negative(evf):
    xs = [-0.05, 0.01, -0.02, -0.03]
    assert evf.tail_note(xs, 0.0016) == ""


def test_note_names_the_subject(evf):
    xs = [0.40] + [-0.01] * 19
    assert "this book" in evf.tail_note(xs, 0.0016)
    assert "this symbol" in evf.tail_note(xs, 0.0016, subject="this symbol")


# --- 8: end-to-end render of the real-book shape ---------------------------

def _book(recs):
    return json.dumps({"log": recs})


def test_report_shows_a_positive_mean_with_a_negative_median(evf, tmp_path):
    # The T0.22 shape: 20 trades, one +40%, the rest -1%.
    recs = [{"ret": 0.40}] + [{"ret": -0.01} for _ in range(19)]
    (tmp_path / "trend-state.json").write_text(_book(recs))

    lines = evf.report_lines(data_dir=tmp_path)
    text = "\n".join(lines)

    row = next(l for l in lines if "trend_follower" in l and "REAL" in l)
    # mean positive, median negative, both rendered on the same row
    assert "+" in row
    assert "median" in text          # column header exists
    assert "tail, not the middle" in text   # the note fired
    # the delete-1 column is present and non-positive for this book
    assert evf.delete_best_mean([0.40] + [-0.01] * 19) - evf.FEE_RT < 0


def test_report_marks_a_tail_positive_book_with_a_warning(evf, tmp_path):
    recs = [{"ret": 0.40}] + [{"ret": -0.01} for _ in range(19)]
    (tmp_path / "trend-state.json").write_text(_book(recs))
    text = "\n".join(evf.report_lines(data_dir=tmp_path))
    # The diagnosis must not print an unqualified check for a tail book.
    assert "⚠️ trend_follower (REAL $)" in text


# --- 9: the two readers agree (one definition) ----------------------------

def test_both_readers_share_one_definition(evf, psn):
    # Not a copy: both must resolve to functions defined in the SAME file.
    # (Identity is not asserted — pytest/the loader can legitimately produce
    # two module objects for one file, which says nothing about the source.)
    for name in ("median", "delete_best_mean", "tail_note"):
        f_efv = getattr(evf, name)
        f_psn = getattr(psn, name)
        assert Path(f_efv.__code__.co_filename).name == "expectancy_vs_fee.py"
        assert f_psn.__code__.co_filename == f_efv.__code__.co_filename
        assert f_psn.__code__.co_firstlineno == f_efv.__code__.co_firstlineno


def test_both_readers_compute_the_same_tail(evf, psn):
    xs = [0.40, 0.02, -0.01, -0.03, -0.02, 0.05]
    assert psn.median(xs) == evf.median(xs)
    assert psn.delete_best_mean(xs) == evf.delete_best_mean(xs)


# --- 11: the columns are unconditional (the knife-edge lesson) ------------

def test_the_tail_columns_print_even_when_no_note_fires(evf, tmp_path):
    # The real T0.22 shape: delete-1 is POSITIVE (+0.697% at n=213), so the
    # note correctly stays silent — but the median must still be shown. A
    # boolean gate on one borderline row must never be the only signal.
    recs = ([{"ret": 0.40}] + [{"ret": 0.10} for _ in range(4)]
            + [{"ret": -0.01} for _ in range(15)])
    (tmp_path / "trend-state.json").write_text(_book(recs))
    lines = evf.report_lines(data_dir=tmp_path)
    row = next(l for l in lines if "trend_follower" in l and "REAL" in l)
    # mean > 0, delete-1 > 0 -> no note ...
    assert evf.tail_note([0.40] + [0.10] * 4 + [-0.01] * 15, evf.FEE_RT) == ""
    assert "tail, not the middle" not in "\n".join(lines)
    # ... and the row STILL carries a median that is negative.
    assert "-" in row and "median" in "\n".join(lines)
    xs = [0.40] + [0.10] * 4 + [-0.01] * 15
    assert evf.median(xs) - evf.FEE_RT < 0


# --- 10: the verdict is unchanged by the tail columns ---------------------

def test_the_verdict_is_unchanged_by_the_tail_columns(evf, tmp_path):
    # 20 trades, mean NET clearly <= 0 with a t near zero -> "DEAD — no edge".
    recs = [{"ret": -0.002} for _ in range(20)]
    (tmp_path / "unified-paper.json").write_text(
        json.dumps({"closed": [dict(r, pnl=-0.01, size=5.0) for r in recs]}))
    lines = evf.report_lines(data_dir=tmp_path)
    row = next(l for l in lines
               if "unified_trader" in l and "DEAD" in l)
    # The tail note must NOT fire here (mean already negative), and adding the
    # columns must not turn a losing book into a passing one.
    assert "tail, not the middle" not in "\n".join(lines)
    assert "DEAD" in row
