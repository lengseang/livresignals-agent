"""A suggestion channel whose items cannot be closed is a fixed cost per run.

Found 2026-11-05. `_suggest()` ended with a hardcoded literal:

    out.append({"priority": 4, "type": "explore",
                "idea": "Explore: time-of-day filter — capitulation bounces may
                         cluster around US session open (13:30-16:00 UTC).
                         Test if a session filter raises win rate."})

It survived from the repo's first commit (`cb1af99`) and was emitted on every
run — every 40 minutes — under the real findings. The volume-gate idea beside
it HAD been closed, but only because a human hand-edited the literal into an
`explore_result`. So "is this idea still open?" was answered by whether an
author remembered to delete a line, not by anything checkable.

The shift tested the hypothesis (verdict: NEGATIVE — no session band survives a
walk-forward split or the removal of 2026-10-18; see
docs/proposals/session-filter-closed-2026-11-05.md) and moved both the list of
ideas and the record of their outcomes into code: `_explorations()` says what
the loop has considered, `CLOSED_HYPOTHESES` says where each one landed.

These tests assert the PROPERTY, not the string: a closed hypothesis is
rendered as a result and can never reappear as an open task even when a caller
passes live-sounding text, and every closure carries evidence someone can go
and read. The first of those is the one that fails on the old code — there, no
register existed and the literal was unconditional.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "improvement_loop", ROOT / "scripts" / "improvement_loop.py")
il = importlib.util.module_from_spec(_SPEC)
sys.modules["improvement_loop"] = il
_SPEC.loader.exec_module(il)


def test_closed_hypothesis_renders_as_result_not_open_task():
    """The register wins over the caller — this is the mutation-sensitive bit.

    A caller asking for a closed key must get the CLOSED rendering even if it
    passes live-sounding text. If someone later "fixes" _emit_explorations() to
    trust its argument, this fails.
    """
    emitted = il._emit_explorations(
        [("session-filter", "Explore: time-of-day filter, still worth a look")]
    )
    assert len(emitted) == 1
    assert emitted[0]["type"] == "explore_result"
    assert "CLOSED" in emitted[0]["idea"]


def test_open_hypothesis_still_emits():
    """The guard must not be a blanket refusal — an open idea still goes out."""
    emitted = il._emit_explorations(
        [("something-new", "Explore: a genuinely new idea")]
    )
    assert len(emitted) == 1
    assert emitted[0]["type"] == "explore"
    assert emitted[0]["idea"] == "Explore: a genuinely new idea"


def test_every_closure_carries_evidence_and_a_verdict():
    """A verdict nobody can check is a rumour with a date on it."""
    assert il.CLOSED_HYPOTHESES, "the register must not be empty"
    for key, entry in il.CLOSED_HYPOTHESES.items():
        assert "verdict" in entry and entry["verdict"].strip(), key
        assert "evidence" in entry and entry["evidence"].strip(), key
        # The verdict must say it is closed and which way it went, so a reader
        # scanning the report cannot mistake it for an open suggestion.
        assert entry["verdict"].upper().startswith("CLOSED"), key
        # and the evidence must be a path that exists, not a plausible name
        assert (ROOT / entry["evidence"]).exists(), (
            f"{key} cites {entry['evidence']!r}, which is not in the repo")


def test_session_filter_is_closed_not_open():
    """The specific hypothesis this shift tested must not reappear as open."""
    assert "session-filter" in il.CLOSED_HYPOTHESES
    verdict = il.CLOSED_HYPOTHESES["session-filter"]["verdict"]
    assert "NEGATIVE" in verdict
    assert "2026-11-05" in verdict


def test_suggest_emits_no_open_explore_for_a_closed_hypothesis():
    """End-to-end through _suggest(), not just the helper.

    _suggest() is fed the smallest viable book; the point is not its verdicts
    but that whatever it returns never contains an open exploration whose
    subject is already closed — and that each closed subject DOES appear once,
    as an explore_result, so the verdict stays visible.
    """
    out = il._suggest(paper=[], live=[], regimes=[], edges=[], edge_meta={})
    # Markers chosen to match the CLOSED verdict text as well as the open text:
    # the session verdict says "session band", not "time-of-day".
    closed_markers = {"session-filter": "session",
                      "volume-spike-gate": "volume-spike"}
    for s in out:
        if s.get("type") == "explore":
            for key, marker in closed_markers.items():
                assert marker not in s.get("idea", ""), (
                    f"closed hypothesis {key!r} re-emitted as open: {s['idea']}")
    for key, marker in closed_markers.items():
        hits = [s for s in out
                if s.get("type") == "explore_result" and marker in s.get("idea", "")]
        assert hits, f"closed hypothesis {key!r} has no visible result entry"
