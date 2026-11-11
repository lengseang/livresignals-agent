"""T0.20: the trend run log must not print `INFO trend @ None` and drop the reason.

Live evidence (2026-11-11, `logs/trend.log`):

    879:  INFO trend @ None
    908:  INFO trend @ None
    916:  INFO trend @ None
    920:  INFO trend @ None

All four sit under an `OPEN <sym>` line. `scripts/trend_follower.py:709` printed
`f"  {kind} {sym} @ {px}"` for every event, so a symbol-less event lost its
symbol slot to the sentinel `"trend"`, its price slot to `None`, and — the actual
defect — its `extra`, which is the whole reason the event exists.

The fix is `event_log_line()`. The controls matter more than the fix: `ERR` is
*not* a notice kind (9 of 11 sites carry a real symbol), so a kind-based rule
would have broken the failure lines. Every test below that asserts the new shape
is paired with one that pins an existing line to its exact old bytes.

Mutations that must turn these red:

  * route on `px is None` instead of `sym is None`   -> test_an_err_with_a_symbol_is_untouched
  * drop `extra` from the notice branch             -> test_info_keeps_its_reason / _err_
  * `str(extra)` in the trade branch                -> test_exit_extra_is_not_printed
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from trading.ops.event_log import NO_REASON, event_log_line  # noqa: E402

SRC = REPO / "scripts" / "trend_follower.py"

# The exact payload from the live run of 2026-11-10 12:10 (trend.log:908).
LIVE_INFO = ("INFO", "trend", None,
             "3 candidate(s) paper-only (not in REAL_SYMS): INJ/USDT, ONE/USDT, PEPE/USDT")


# --- the defect -----------------------------------------------------------

def test_info_keeps_its_reason():
    """The live INFO line: reason present, no `None`, no `@`."""
    line = event_log_line(*LIVE_INFO)
    assert "paper-only" in line, line
    assert "INJ/USDT, ONE/USDT, PEPE/USDT" in line, line
    assert "None" not in line, f"a sentinel leaked into the log: {line!r}"
    assert " @ " not in line, f"a symbol-less event still printed a price slot: {line!r}"


def test_a_symbolless_err_keeps_its_reason():
    """The kill-switch ERR (`trend_follower.py:645`) has no symbol either."""
    line = event_log_line("ERR", "trend", None,
                          "entry blocked: config/LIVE_ENABLED absent (central live switch)")
    assert "LIVE_ENABLED absent" in line, line
    assert "None" not in line, line
    assert " @ " not in line, line


def test_a_missing_reason_says_so_rather_than_none():
    for extra in (None, "", "   "):
        line = event_log_line("INFO", None, None, extra)
        assert line == f"  INFO {NO_REASON}", f"{extra!r} -> {line!r}"
        assert "None" not in line


def test_an_empty_symbol_is_a_notice():
    assert event_log_line("INFO", "", None, "why") == "  INFO why"
    assert event_log_line("INFO", "   ", None, "why") == "  INFO why"


# --- controls: nothing that already works may change ----------------------

def test_an_err_with_a_symbol_is_untouched():
    """`ERR` is not a notice kind. 9 of its 11 sites carry a real symbol.

    This is the control the first draft of the design would have failed: routing
    by kind reformats (and de-reasons) the failure lines the operator reads.
    """
    line = event_log_line("ERR", "INJ/USDT", None, "sell failed (attempt 2): timeout")
    assert line == "  ERR INJ/USDT @ None", line


@pytest.mark.parametrize("kind,sym,px", [
    ("OPEN", "ONE/USDT", 0.001866),
    ("EXIT", "INJ/USDT", 7.787),
    ("REALBUY", "BTC/USDT", 80000.0),
    ("REALEXIT", "SOL/USDT", 1.051),
])
def test_position_events_are_byte_identical(kind, sym, px):
    """Today's format, exactly: `  KIND <sym> @ <px>`."""
    assert event_log_line(kind, sym, px, {"ret": 1.2, "why": "x", "real": True}) \
        == f"  {kind} {sym} @ {px}"


def test_exit_extra_is_not_printed():
    """`EXIT` passes a dict in `extra`; `str(dict)` must never reach the log."""
    line = event_log_line("EXIT", "ONE/USDT", 0.001786,
                          {"ret": -3.4, "why": "trend broke", "real": False})
    assert "{" not in line and "trend broke" not in line, line


# --- the source sweep: every symbol-less site must carry a reason ---------

_APPEND = re.compile(r"events\.append\(\(")
# A tuple whose symbol slot names no instrument: `None` or the sentinel string.
# The symbol slot is a literal in every call site, which is what makes this
# readable as source text.
_SYMBOL_LESS = re.compile(
    r'events\.append\(\(\s*"(?P<kind>[A-Z]+)"\s*,\s*'
    r'(?:None|"(?P<sentinel>trend)")\s*,\s*None\s*,\s*'
    r'(?P<extra>None|""|\'\'|[^)]*?)\s*\)',
    re.S,
)


def test_every_symbol_less_event_site_has_a_reason():
    """A symbol-less event whose `extra` is empty is a notice with nothing to say.

    Read as source text: `trend_follower.py` is never imported (it must not run),
    and this stays true as the file grows.
    """
    src = SRC.read_text(encoding="utf-8")
    assert _APPEND.search(src), "the events.append(( call sites moved: update this sweep"

    offenders = [
        m.group(0).replace("\n", " ")
        for m in _SYMBOL_LESS.finditer(src)
        if (m.group("extra") or "").strip() in ("None", "", '""', "''")
    ]
    assert not offenders, (
        "symbol-less event(s) with no reason — the log would print "
        "`KIND (no reason recorded)`: " + repr(offenders)
    )


def test_the_sweep_can_see_the_live_sites():
    """Control for the sweep above: it must FIND the three real symbol-less sites.

    A regex that finds nothing passes for the wrong reason.
    """
    src = SRC.read_text(encoding="utf-8")
    kinds = [m.group("kind") for m in _SYMBOL_LESS.finditer(src)]
    # `INFO` x1 (:653) + `ERR` x2 (:640, :645). If this drops, the sweep went blind.
    assert kinds.count("INFO") >= 1, kinds
    assert kinds.count("ERR") >= 2, kinds
