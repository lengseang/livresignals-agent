"""A lesson narrative that quotes the ledger must GO STALE OUT LOUD.

Found 2026-10-19. Commit 0c00a42 (trailed-sl RR artefact) retracted the
"RR<2 loses, skip it" advice — correctly — and replaced it with this sentence,
hardcoded in `improve()` and, near-verbatim, in `write_rollup()`:

    "it is +9.95 in the first half and -1.20 in the second, so RR does not
     separate this book at all"

Those numbers are a FACT about a ledger of 3,171 lessons on 2026-10-18. They
were written as a string constant. One day and 24 lessons later the same
computation over the same ledger (3,196 lessons) returns **+11.17 / +0.04** —
the second half is now net-POSITIVE, so the "first half pays, second half
doesn't" shape that justified "RR does not separate this book" has dissolved.

The sentence is not wrong in its conclusion (RR still does not separate the
book: the two halves' SIGNS now agree, and the around-2.0 band still does not
clear the fee). What is wrong is that the evidence printed next to it is no
longer the evidence. A reader who checks it is told their own ledger disagrees
with the note. That is the failure mode this repo's Rule 7 is about, in prose
form: a threshold/number quoted at a level where the population has moved.

15 monthly cuts of the split point shift the second-half net by ~$18 depending
on where it lands — because most exact-2.0 fills at the same instant differ by
under a second, a split by PnL lands *inside* one trade cluster and either
counts or omits up to 4 near-identical fills. So the split is only reported
when it is ORDER-INDEPENDENT: computed on trade time, with index order kept as
a cross-check, and suppressed if the two orderings disagree.

Pinned below:
  1. the live ledger's actual halves disagree with the string in the source
     (this is the regression — it fails until the numbers are derived);
  2. the derivation is order-independent, and returns None rather than a
     number when it is not;
  3. the narrative and the rollup print the DERIVED numbers, not a constant;
  4. if the halves ever do go stale again, the sentence says so instead of
     quoting them as current.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path("/opt/trading-agent")
LOOP = Path("/home/ubuntu/hermes-home/scripts/trade_lesson_loop.py")

pytestmark = pytest.mark.skipif(
    not LOOP.exists(), reason="agent-home lesson loop not present in this checkout")

VALID_POP = {"instrument_invalid": False, "instrument_evidence": {}}


def _load_loop():
    spec = importlib.util.spec_from_file_location("trade_lesson_loop", LOOP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["trade_lesson_loop"] = mod
    spec.loader.exec_module(mod)
    return mod


def _lesson(rr, pnl, ts):
    """A lesson as the ledger stores the fields rr_split reads."""
    return {"rr": rr, "pnl": pnl, "trade_ts_utc": ts,
            "verdict": "ALIGNED" if pnl > 0 else "AS DESIGNED"}


def _ledger():
    p = ROOT / "data/trade-lessons.jsonl"
    if not p.exists():
        pytest.skip("lesson ledger not present")
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def test_the_hardcoded_halves_no_longer_describe_the_live_ledger():
    """The regression itself: source string says +9.95/-1.20; ledger says otherwise."""
    loop = _load_loop()
    split = loop.rr_split(_ledger())
    # `None` is a legitimate answer: the guard refuses a cut whose halves move
    # when the ledger is re-ordered, and on 2026-10-19 it does (settlement order
    # lags trade time). The claim tested here is that NO number is hardcoded —
    # a refused split must not resurrect one either.
    if split is not None:
        assert (split["first_net"], split["second_net"]) != (9.95, -1.2), (
            "ledger still reports the 2026-10-18 halves; remove this test only "
            "if the numbers are DERIVED, not re-hardcoded"
        )
    src = LOOP.read_text()
    # A stale pair may be DOCUMENTED (module docstring, comments), but it must not
    # appear inside the body of a function that prints to a reader. Checking the
    # whole file failed to catch the mutation because the docstring above quotes
    # the same sentence; so strip docstrings and comments, then look.
    tree = ast.parse(src)
    bodies = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            # drop a leading docstring expression
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    getattr(body[0], "value", None), ast.Constant) and isinstance(
                    body[0].value.value, str):
                body = body[1:]
            bodies[node.name] = "\n".join(ast.unparse(b) for b in body)
    offenders = [name for name, txt in bodies.items()
                 if "9.95" in txt and "first half" in txt]
    assert not offenders, (
        f"{offenders} still quote the stale halves in printable code")
    # And the numbers must actually come from the derivation, in both producers.
    assert "rr_split_text(" in bodies.get("improve", ""), (
        "improve() no longer builds its RR sentence from rr_split_text()")
    assert "rr_split_text(" in bodies.get("write_rollup", ""), (
        "write_rollup() no longer builds its RR implication from rr_split_text()")


def test_split_is_order_independent():
    """Rebuild the same ledger in the other order — the halves must not move.

    Index order is what groups the sub-second exact-2.0 cluster; trade time is
    what the ledger is actually ordered by. If a split point can fall inside one
    instant, the two orders disagree and rr_split must refuse to report.
    """
    loop = _load_loop()
    rows = _ledger()
    a = loop.rr_split(rows)
    if a is not None:
        b = loop.rr_split(sorted(rows, key=lambda r: str(r.get("trade_ts_utc") or "")))
        assert b is not None
        assert (a["first_n"], a["second_n"]) == (b["first_n"], b["second_n"])
        assert round(a["first_net"], 2) == round(b["first_net"], 2)
        assert round(a["second_net"], 2) == round(b["second_net"], 2)


def test_split_refuses_a_ledger_it_cannot_cut_cleanly():
    """One instant holding most of the book: any cut inside it is arbitrary."""
    loop = _load_loop()
    rows = [_lesson(1.5, -0.01, "2026-10-01T00:00:00+00:00") for _ in range(300)]
    assert loop.rr_split(rows) is None


def test_the_narrative_prints_the_derived_numbers_not_a_constant():
    loop = _load_loop()
    split = loop.rr_split(_ledger())
    t = {"symbol": "XRP/USDT", "ts_utc": "2026-10-18T00:00:00+00:00",
         "entry": 1.0, "tp": 1.015, "sl": 0.99, "pnl": -0.01, "reason": "TIME",
         "loose": True}
    note = " ".join(loop.improve(t, "AS DESIGNED", VALID_POP, split=split))
    assert "+9.95" not in note
    if split is not None:
        assert f"{split['first_net']:+.2f}" in note
        assert f"{split['second_net']:+.2f}" in note
    else:
        assert "no half-split" in note, (
            "the ledger refused a clean cut, so the note must say so rather "
            "than print a number")


def test_halves_that_agree_are_not_quoted_as_a_decay_story():
    """If both halves pay (or both lose), the sentence must not claim a split."""
    loop = _load_loop()
    split = {"n": 2000, "first_n": 1000, "second_n": 1000,
             "first_net": 8.0, "second_net": 2.0, "cut_ts": "2026-10-01T00:00:00+00:00",
             "order_independent": True}
    text = loop.rr_split_text(split)
    assert "+8.00" in text and "+2.00" in text
    assert "did NOT hold" in text
    assert "decay claim is stale" in text


def test_the_live_sentence_states_what_the_live_halves_actually_do():
    """Whatever shape the ledger has today, the sentence must match it."""
    loop = _load_loop()
    split = loop.rr_split(_ledger())
    text = loop.rr_split_text(split)
    if split is None:
        # Refusing to cut is a statement too, and it must not imply a shape.
        assert "no half-split" in text and "claim is made either way" in text
    elif split["first_net"] > 0 > split["second_net"]:
        assert "shape is still present" in text
    else:
        assert "did NOT hold" in text, (
            "halves no longer decay yet the sentence still implies they do")


def test_missing_split_falls_back_without_inventing_a_number():
    loop = _load_loop()
    t = {"symbol": "XRP/USDT", "ts_utc": "2026-10-18T00:00:00+00:00",
         "entry": 1.0, "tp": 1.015, "sl": 0.99, "pnl": -0.01, "reason": "TIME",
         "loose": True}
    note = " ".join(loop.improve(t, "AS DESIGNED", VALID_POP, split=None))
    assert "does not separate" in note.lower() and "no half-split" in note.lower()
    for stale in ("+9.95", "-1.20"):
        assert stale not in note


def test_the_repairable_ledger_prints_a_single_clean_rr_sentence():
    """A repaired lesson must read once, not nest a second full explanation."""
    loop = _load_loop()
    t = {"symbol": "XRP/USDT", "ts_utc": "2026-10-18T00:00:00+00:00",
         "entry": 1.0, "tp": 1.015, "sl": 0.99, "pnl": -0.01, "reason": "TIME",
         "loose": True}
    note = " ".join(loop.improve(t, "AS DESIGNED", VALID_POP))
    assert note.count("RR does not separate this book") == 1, note
    # Either one quoted pair, or the explicit refusal — never two explanations.
    assert note.count("first half") + note.count("no half-split") == 1, note
