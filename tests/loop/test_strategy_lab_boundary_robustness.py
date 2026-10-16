"""strategy_lab's boundary-robustness gate (rule 5), added 2026-10-16.

THE DEFECT (found 2026-10-16)
-----------------------------
The lab gated a candidate on "positive in BOTH halves" measured at a SINGLE
split point: `half = len(rows) // 2` of a rolling 4000-candle window. That is one
arbitrary line, not a test of an edge. Measured on the 7 published SOL candidates:
shifting the boundary by +/-400 candles (~17 days on 1h) flipped **5 of 7** second
halves negative — the pass depended on where the line was drawn.

The fix: re-measure at SPLIT_SHIFTS = (0, -400, +400, -800, +800) and require
`worst_half_net > 0` across all of them. A candidate fragile at any tested split
is rejected and recorded with the offence.

These tests use a synthetic in-memory strategy evaluator fixture, so they run
without the live candle DB or the lab's production constants.

The lab script lives OUTSIDE the repo (Hermes-side, ~/hermes-home/scripts). The
suite skips cleanly when it is absent (e.g. off-VPS CI), the same shape as the
gate's own sanctioned skips.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

LAB_PATH = Path("/home/ubuntu/hermes-home/scripts")

if not (LAB_PATH / "strategy_lab.py").exists():
    pytest.skip("strategy_lab.py is Hermes-side only; not present",
                allow_module_level=True)

sys.path.insert(0, str(LAB_PATH))

import strategy_lab as L  # noqa: E402


def _rows(closes: list[float]):
    """Rows shaped like the candle query result: (open_time, o, h, l, c).

    open=high=low=close keeps the fill exactly `close`, so the outcome is a pure
    function of the price path and the fixture is deterministic.
    """
    return [(i, c, c, c, c) for i, c in enumerate(closes)]


def _up_then_down(n_up: int, n_down: int, up: float = 0.5) -> list[float]:
    """A clean rise (breakouts hit TP) then a fall (breakouts hit SL)."""
    px = [100.0]
    for _ in range(n_up):
        px.append(px[-1] * (1 + up / 100))
    for _ in range(n_down):
        px.append(px[-1] * (1 - up / 100))
    return px


def test_boundary_robust_pass_is_deterministic():
    rows = _rows(_up_then_down(2200, 1000, up=1.0))
    a = L.split_validate(rows, 12, 24, 1.5, L.SPLIT_SHIFTS)
    b = L.split_validate(rows, 12, 24, 1.5, L.SPLIT_SHIFTS)
    assert a == b, "the same input must give the same boundaries"
    assert len(a) == 5, f"expected 5 tested splits, got {len(a)}"


def test_boundary_robust_flags_a_fragile_candidate():
    """A candidate whose edge lives entirely in one half is fragile.

    Rising first (breakouts win) then falling (breakouts lose) makes the first
    half positive and the second negative; shifting the boundary up must expose
    the negative second half somewhere.
    """
    rows = _rows(_up_then_down(2200, 1000, up=1.0))
    bounds = L.split_validate(rows, 12, 24, 2.0, L.SPLIT_SHIFTS)
    worst = min(min(h1, h2) for _h, (h1, h2) in bounds)
    assert worst <= 0, (
        "a path whose edge is in one half must have a non-positive half "
        f"at some boundary; got bounds={bounds}"
    )


def test_boundary_robust_accepts_a_uniform_edge():
    """A monotone path where every window is positive should stay positive."""
    # A single long steady rise: any breakout enters, any forward window rises
    # enough to hit a 1.5x-of-2%-stop target before the 2% stop.
    px = [100.0]
    for _ in range(3200):
        px.append(px[-1] * 1.002)
    rows = _rows(px)
    bounds = L.split_validate(rows, 12, 24, 1.5, L.SPLIT_SHIFTS)
    assert len(bounds) == 5, f"fixture must be long enough for all shifts; got {bounds}"
    assert all(h1 > 0 and h2 > 0 for _h, (h1, h2) in bounds), (
        f"a uniformly rising path should clear every boundary; got {bounds}"
    )


def test_split_validate_legacy_shape():
    """Calling without strategy params still returns (split, (a, b)) slices."""
    rows = _rows([100.0 + i for i in range(120)])
    out = L.split_validate(rows)
    assert len(out) == 1
    h, (a, b) = out[0]
    assert h == 60 and len(a) == 60 and len(b) == 60


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
