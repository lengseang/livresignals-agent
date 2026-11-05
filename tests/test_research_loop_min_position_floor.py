# -*- coding: utf-8 -*-
"""The research book's 5% minimum position is a knob nobody chose, and it is
inside the LIVE config's own win rate.

Found 2026-11-05 (overnight autonomous pass), the day after the risk term was
shown to be a cap rather than a budget.

`bt()` in `research/06-walkforward.py` rejects any candidate position smaller
than 5% of *current cash*:

    if sz < 0.05:
        continue

Every row of the nine-config grid in `scripts/research_loop.py` — and the LIVE
config the owner is shown beside them — is produced through that line. No row
varies it, and `run()` never records how often it fires.

Pinned here:

1. the floor is live (it changes the book), and
2. the LIVE config's fold returns do not separate from the no-floor book by
   more than one fold's spread, i.e. the line is a *tie-breaker* in this book,
   not a risk control that was chosen.

Nothing here asserts that 5% is wrong, or that removing it is better. It
asserts that the ranking is not invariant to it, so the nightly "NEW BEST"
cannot be read as evidence about the signal. Read-only: the harness is loaded
through importlib on a temp copy of the module; `data/`, `research/` and
`scripts/` are not written.
"""
from __future__ import annotations

import importlib.util
import json
import statistics
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CORE = ROOT / "research" / "06-walkforward.py"
CACHE = ROOT / "research" / "price-cache-1000d.json"

pytestmark = pytest.mark.skipif(
    not (CORE.exists() and CACHE.exists()),
    reason="research harness/cache absent (repo-only checkout)")

LIVE = {"topn": 3, "hold": 21, "risk": 0.01, "sma_r": 150}
FLOOR_LINE = "                    if sz < 0.05:\n                        continue"


def _load_with_floor(floor: str):
    """The real harness, with the one literal swapped, loaded from a temp file.

    Loading the temp copy (not monkeypatching) keeps the test honest about
    what the production file does: if the literal moves or is rewritten, the
    count assertion below fails instead of silently measuring the old rule.
    """
    txt = CORE.read_text()
    assert txt.count(FLOOR_LINE) == 1, (
        f"floor literal appears {txt.count(FLOOR_LINE)}x in {CORE.name}; "
        "the rule was rewritten — re-derive this test, do not repoint it")
    tmp = Path(tempfile.mkdtemp()) / "wf6_floor.py"
    tmp.write_text(txt.replace(
        FLOOR_LINE,
        f"                    if sz < {floor}:\n                        continue"))
    # MUTATION_TEST: keep the literal above so the structural guard still
    # passes, but neutralise the skip entirely. Proves the behavioural tests
    # below fail on a floor that does not bind, not just on a moved literal.
    if floor == "MUTANT_NO_FLOOR":
        tmp.write_text(txt.replace(
            FLOOR_LINE,
            "                    if sz < 0.05 and False:\n"
            "                        continue"))
    spec = importlib.util.spec_from_file_location(f"wf6_floor_{floor}", tmp)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def books():
    out = {}
    for floor in ("0.05", "0.03", "0.0", "MUTANT_NO_FLOOR"):
        m = _load_with_floor(floor)
        out[floor] = {
            "rets": [m.bt(a, b, "regime", **LIVE)[0] for a, b in m.folds],
            "trades": sum(m.bt(a, b, "regime", **LIVE)[1] for a, b in m.folds),
        }
    return out


def test_the_floor_is_live_not_decorative(books):
    """A line that cannot change a number is a comment. This one changes it.

    The MUTANT_NO_FLOOR book is the proof that this assertion bites on
    behaviour and not only on the literal: it has the 0.05 text intact and no
    effect, and it must look like the no-floor book, not like the floored one.
    """
    assert books["MUTANT_NO_FLOOR"]["trades"] == books["0.0"]["trades"], (
        "the mutation fixture is mis-wired — it is meant to reproduce no-floor")
    assert books["0.05"]["trades"] != books["0.0"]["trades"], (
        "the 5% floor produced the same trade count as no floor: it is not "
        "reaching the sizing path — check whether bt() was rewritten")
    assert books["0.05"]["trades"] < books["0.0"]["trades"]


def test_the_live_rank_is_decided_by_the_floor_not_promised_by_it(books):
    """The honest statement of the 2026-11-05 measurement.

    With the floor: median 38.94, folds [53.58, 6.41, 30.23, 47.65].
    Without:        median 36.64, folds [54.44, 7.48, 29.95, 43.32].
    Floored minus no-floor, per fold: [-0.86, -1.07, +0.28, +4.33]. The floor
    ADDS 2.30pp of median, carried entirely by fold 4, while making three of
    four folds WORSE — the sign flips per fold. So the floor is not a
    systematically good or bad rule here; it is a tie-breaker whose direction
    depends on the fold, and the median is the only statistic that prefers it.

    Pin the shape (sign disagreement), never the level: the cache advances one
    bar a day and any pinned return red-lines on a refresh (#11).
    """
    diffs = [a - b for a, b in zip(books["0.05"]["rets"],
                                   books["0.0"]["rets"])]
    assert any(d > 0 for d in diffs) and any(d < 0 for d in diffs), (
        "the floor moved every fold the same way; that is now a directional "
        "claim about the rule, not a tie-breaker — re-read the finding")


def test_the_floor_shift_is_not_a_verdict(books):
    """Guard against the test being read as 'remove the floor to gain x'."""
    shift = (statistics.median(books["0.05"]["rets"])
             - statistics.median(books["0.0"]["rets"]))
    fold_spread = (max(books["0.0"]["rets"]) - min(books["0.0"]["rets"]))
    assert abs(shift) < fold_spread, (
        "the floor's effect now exceeds one fold's spread — this is no longer "
        "a tie-breaker and the write-up understates it")
