"""The run line for one trend event — including the kind that has no symbol.

`scripts/trend_follower.py` prints every event with one format:

    for kind, sym, px, extra in events:
        print(f"  {kind} {sym} @ {px}")

`OPEN`, `EXIT`, `REALBUY` and most `ERR` events fit that shape. Three do not:
`INFO` (`trend_follower.py:653`) and two `ERR` sites (`:640`, `:645`) carry no
symbol — the second tuple slot is the literal sentinel ``"trend"``, the price is
`None`, and the reason lives in `extra`. Printed through the trade format they
become:

      INFO trend @ None

Measured on the live host 2026-11-11: four such lines in `logs/trend.log`, every
one directly under an `OPEN <sym>` line, and the reason — which is the entire
content of the event — never reaches the log at all.

This module is the pure formatter. A pure function because it must be testable
without an exchange, a state file or an order path, and because
`scripts/trend_follower.py` is on the loop's never-edit list: the owner applies
the one-line call-site change (see `docs/loop/ESCALATIONS.md`, E16).

The discriminator is **not the kind**. `ERR` is not a notice kind: 9 of its 11
call sites pass a real symbol and a reason, and those lines
(`  ERR INJ/USDT @ None`) are what the operator already reads. Nor is it
"`sym` is `None`", which is what the first draft assumed — the emitter never
passes `None` there, it passes the sentinel string ``"trend"``. That is exactly
the class of assumption that let `INFO trend @ None` survive a shipped fix and a
green test run, so the sentinel is named here instead of inferred.
"""
from __future__ import annotations

__all__ = ["event_log_line", "NO_REASON", "NO_SYMBOL"]

#: The emitter's stand-in for "this event names no symbol" (`trend_follower.py`
#: :640, :645, :653). Not `None` — a sentinel string, which is why a truthiness
#: test cannot see it.
NO_SYMBOL = "trend"

#: What a notice with an empty reason prints. A missing value is honest
#: (LOOP.md §3 rule 2); `None` in a human-facing line is not.
NO_REASON = "(no reason recorded)"


def event_log_line(kind: str, sym, px, extra) -> str:
    """Render one event exactly as the run log should show it.

    Two shapes, chosen by whether the event names a symbol:

    * **symbol** → ``"  KIND <sym> @ <px>"`` — byte-identical to the format
      `trend_follower.py` has always used, so existing greps and quoted log
      lines do not change. A `None` price here is the ordinary failure shape
      and prints ``@ None`` as before.
    * **no symbol** (``None``, empty, or :data:`NO_SYMBOL`) →
      ``"  KIND <reason>"`` — the reason from ``extra``, or :data:`NO_REASON`
      when there is none.
    """
    if _has_no_symbol(sym):
        return f"  {kind} {_reason_text(extra)}"
    return f"  {kind} {sym} @ {px}"


def _has_no_symbol(sym) -> bool:
    return sym is None or (isinstance(sym, str) and sym.strip() in ("", NO_SYMBOL))


def _reason_text(extra) -> str:
    """The notice's reason as one non-empty line."""
    if isinstance(extra, dict):
        # No notice site passes a dict today; if one ever does, naming the keys
        # beats `str(dict)` leaking braces into the log.
        return " ".join(f"{k}={v}" for k, v in extra.items()) or NO_REASON
    if extra is None:
        return NO_REASON
    return str(extra).strip() or NO_REASON
