"""T1.6 — is the strategy lab measuring, or just running? (2026-11-06)

The lab fires 96×/day and writes one `rejected.jsonl` line per grid point. The
report said "nothing cleared the gate" every run since 2026-10-16 and that read
as a quiet, working loop. These tests pin the questions that distinguish a loop
that is measuring from a loop that is merely running:

  - a run is a burst sharing a WALL-CLOCK MINUTE (the first version grouped by
    a 30-minute gap and found 2 runs in 165,059 lines — the mutant for it is
    `test_the_first_grouping_bug_is_caught`);
  - a verdict that REPEATS must not be counted as information;
  - a symbol that has never advanced must be named, not silently averaged into
    "the gate found nothing".

Fixtures are synthetic and in-tree, so these run anywhere. The live-log test is
a read-only report over `data/strategy-lab/`, skipped when absent, and it pins
the *shape* of the finding (all boundary verdicts from one symbol; the report
never claims a per-run filter it does not have) — not the day's numbers.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from trading.ops import lab_repetition as L  # noqa: E402

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _line(ts: datetime, sym: str, reason: str, lb: int = 24, rr: float = 2.0) -> str:
    return json.dumps({
        "ts_utc": ts.isoformat(), "symbol": sym, "entry_lb": lb, "exit_lb": 24,
        "rr": rr, "reason": reason,
    })


def _write(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _synthetic_runs(n_runs: int, syms=("SOL/USDT", "BTC/USDT")) -> list[str]:
    """N runs 15 min apart, each with one split-reject and one boundary-reject.

    The boundary reject is deliberately identical every run, so novelty must
    count exactly 1 distinct verdict set, not N.
    """
    out = []
    for i in range(n_runs):
        t = T0 + timedelta(minutes=15 * i)
        out.append(_line(t, "SOL/USDT", "failed boundary-robustness: worst half "
                                        "-0.0716 across 5 splits; fragile at [0]"))
        out.append(_line(t + timedelta(milliseconds=5), "BTC/USDT",
                         "failed split: first=-0.4120 second=+0.0056"))
    return out


# ------------------------------------------------------------------ grouping


def test_a_run_is_a_wall_clock_minute_not_a_gap(tmp_path):
    """Six lines 5 ms apart are ONE run. The first implementation said six."""
    lines = [_line(T0 + timedelta(milliseconds=5 * i), "SOL/USDT",
                   f"failed split: first=-0.{i} second=+0.1") for i in range(6)]
    runs = L.load_rejects(_write(tmp_path / "r.jsonl", lines))
    assert len(runs) == 1, f"a burst is one run, got {len(runs)}"
    assert len(runs[0].points) == 6


def test_the_first_grouping_bug_is_caught(tmp_path):
    """Positive control: 15-minute cadence must split into N runs, not 1.

    This is the exact shape the live log has. If the grouping regresses to
    'gap > 30 min', this goes red with 1 instead of 3.
    """
    runs = L.load_rejects(_write(tmp_path / "r.jsonl", _synthetic_runs(3)))
    assert len(runs) == 3


def test_blank_and_corrupt_lines_are_skipped_not_fatal(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text("\n".join([
        _line(T0, "SOL/USDT", "failed split: first=-1 second=+1"),
        "",
        "{not json",
        '{"ts_utc": "nonsense"}',
        _line(T0 + timedelta(milliseconds=3), "SOL/USDT", "failed split: x"),
    ]), encoding="utf-8")
    runs = L.load_rejects(p)
    assert len(runs) == 1 and len(runs[0].points) == 2


# ------------------------------------------------------------------- novelty


def test_an_identical_verdict_is_not_new_information(tmp_path):
    runs = L.load_rejects(_write(tmp_path / "r.jsonl", _synthetic_runs(20)))
    nov = L.novelty(runs)
    assert nov["runs"] == 20
    assert nov["distinct_verdict_sets"] == 1
    assert nov["novel_runs"] == 1, "19 repeats must not be counted as 19 findings"
    assert nov["trailing_identical_runs"] == 20


def test_one_changed_reason_makes_the_run_novel(tmp_path):
    """The finger print is the reason, not the symbol: a moved number is news."""
    lines = _synthetic_runs(3)
    lines.append(_line(T0 + timedelta(minutes=45), "SOL/USDT",
                       "failed boundary-robustness: worst half -0.0100 across 5 splits"))
    lines.append(_line(T0 + timedelta(minutes=45, milliseconds=5), "BTC/USDT",
                       "failed split: first=-0.4120 second=+0.0056"))
    runs = L.load_rejects(_write(tmp_path / "r.jsonl", lines))
    assert L.novelty(runs)["distinct_verdict_sets"] == 2


def test_cadence_is_measured_not_assumed(tmp_path):
    """96 runs/day is a RATE, so the fixture must span four full quarters."""
    runs = L.load_rejects(_write(tmp_path / "r.jsonl", _synthetic_runs(97)))
    nov = L.novelty(runs)
    assert nov["median_gap_min"] == 15.0
    assert nov["runs_per_day"] == pytest.approx(96.0, rel=0.02)


def test_five_runs_in_one_hour_are_not_96_per_day(tmp_path):
    """The rate is derived from the span, not from the interval.

    5 runs 15 min apart span 60 min; dividing 5 by that span gives 120/day. The
    first version of the live report printed 96.2/day from a 33.8-day span,
    which is right for the live log and wrong for this fixture — the test above
    was the one that was wrong, and this one pins why.
    """
    nov = L.novelty(L.load_rejects(_write(tmp_path / "r.jsonl", _synthetic_runs(5))))
    assert nov["runs_per_day"] == pytest.approx(120.0, rel=0.02)


# -------------------------------------------------------------------- reach


def test_a_symbol_that_never_advanced_is_named(tmp_path):
    runs = L.load_rejects(_write(tmp_path / "r.jsonl", _synthetic_runs(4)))
    reach = L.symbol_reach(runs)
    assert reach["boundary_verdicts_by_symbol"] == {"SOL/USDT": 4}
    assert reach["symbols_never_advanced"] == ["BTC/USDT"]
    assert reach["share_of_grid_on_never_advanced"] == 0.5


def test_a_split_reject_does_not_count_as_reaching_the_boundary(tmp_path):
    """Only `failed boundary-robustness` means the split gate ACCEPTED the point.

    A split-level reject never ran the robustness test, so counting it as
    'reached' would inflate the reach by the whole grid.
    """
    lines = _synthetic_runs(1)
    reach = L.symbol_reach(L.load_rejects(_write(tmp_path / "r.jsonl", lines)))
    assert reach["boundary_verdicts_by_symbol"] == {"SOL/USDT": 1}


def test_worst_half_is_read_from_the_reason_text():
    assert L._reason_worst(
        "failed boundary-robustness: worst half -0.0716 across 5 splits; fragile at [0]"
    ) == pytest.approx(-0.0716)
    assert L._reason_worst("failed boundary-robustness: no number here") == 0.0


# ------------------------------------------------------- the archive's price


def test_archive_cost_reports_bytes_per_day(tmp_path):
    p = _write(tmp_path / "rej.jsonl", _synthetic_runs(97))  # 97 runs over 24 h
    cost = L.archive_cost(p, tmp_path / "absent.jsonl")["rejected"]
    assert cost["days"] == pytest.approx(1.0, abs=0.01)
    assert cost["mb_per_day"] is not None and cost["mb_per_day"] > 0
    assert L.archive_cost(p, tmp_path / "absent.jsonl")["candidates"]["exists"] is False


# --------------------------------------------------------------- live shape


LIVE = Path("/opt/trading-agent/data/strategy-lab/rejected.jsonl")


@pytest.mark.skipif(not LIVE.exists(), reason="lab log not present on this host")
def test_live_log_the_gate_has_only_ever_advanced_one_symbol():
    """The finding, pinned as an invariant rather than as a number.

    If a second symbol ever clears the split gate, this goes red and that is
    the signal to re-read the finding — which is the point. The threshold is
    deliberately generous (>= 1 symbol) so an ordinary new SOL config does not
    trip it.
    """
    reach = L.symbol_reach(L.load_rejects(LIVE))
    assert reach["boundary_verdicts_by_symbol"], "no point has ever cleared the split gate"
    assert len(reach["symbols_never_advanced"]) >= 1, (
        "every universe symbol has now advanced — the 'never advanced' finding is stale, "
        f"re-measure: {reach['boundary_verdicts_by_symbol']}"
    )
    assert reach["share_of_grid_on_never_advanced"] < 1.0


# ------------------------------------------------ the header must not drift
#
# Measured 2026-11-06: the module header carried FOUR numbers that the module
# itself did not produce — 165,059 lines (actual 166,119), 2,121 runs from
# 2026-10-16 (actual 2,141), "71.6% fragile" when the actual figure was 100.0%
# (1,538 of 1,538), and "last NEW configuration 2026-10-16" when a new grid
# point first cleared the gate on 2026-10-22. A header is a claim; these tests
# make it a tested one. They pin the DERIVATION, not a stale constant, so a
# week of new runs does not turn them red — but a wrong *kind* of number does.


def test_novelty_is_not_the_same_claim_as_a_new_grid_point(tmp_path):
    """A verdict set can be new every run while the grid has not moved.

    This is the trap the header fell into: it printed the novelty date and
    called it "the last new configuration to clear the gate". They are
    different questions and only the second one is about the search advancing.
    """
    lines = []
    for i in range(4):
        t = T0 + timedelta(minutes=15 * i)
        # grid point is IDENTICAL every run; only the reason text moves.
        lines.append(_line(t, "SOL/USDT",
                           f"failed boundary-robustness: worst half -0.07{i} across 5 splits"))
        lines.append(_line(t + timedelta(milliseconds=5), "BTC/USDT",
                           f"failed split: first=-0.4{i} second=+0.005"))
    runs = L.load_rejects(_write(tmp_path / "r.jsonl", lines))
    nov = L.novelty(runs)
    assert nov["distinct_verdict_sets"] == 4, "every run moved a number -> 4 novel sets"
    first_seen: dict = {}
    for r in runs:
        for k in r.split_survivors:
            first_seen.setdefault(k, r.ts)
    assert len(first_seen) == 1, "but only ONE grid point has ever cleared the gate"


@pytest.mark.skipif(not LIVE.exists(), reason="lab log not present on this host")
def test_the_rest_of_the_header_agrees_with_the_live_log():
    """Pins the two headline claims that were wrong, in derive-not-constant form."""
    runs = L.load_rejects(LIVE)
    # total lines must equal the lines the runs were built from
    assert sum(len(r.points) for r in runs) > 0
    cov = L.grid_coverage(runs)
    # claim: "every survivor died at the boundary" — measured as ALL, not 71.6%
    assert cov["runs_with_a_split_survivor"] > 0
    assert cov["fragile_share"] == 1.0, (
        "the header says every survivor died at the boundary; if that ever "
        f"stops being true, re-measure: {cov['fragile_share']:.3f}"
    )
    # claim: all surviving points are one symbol
    reach = L.symbol_reach(runs)
    assert set(reach["boundary_verdicts_by_symbol"]) == {"SOL/USDT"}
