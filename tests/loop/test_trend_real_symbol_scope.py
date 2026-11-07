"""T0.15: the paper universe grew 38 coins; the real-money universe did not move.

`trend_follower.py` holds two lists:

    SYMS       — 53 USDT pairs the strategy STUDIES (expanded 2026-09-17)
    REAL_SYMS  — 15 pairs real money may OPEN

The entry loop iterates `candidates` drawn from the full paper universe and
`continue`s on anything not in `REAL_SYMS`. The printed line shows the PAPER
held list, so the run reads as if it is acting on all of them. Measured
2026-11-07 on the live host:

    [2026-11-07 12:10] TREND v2 | long 3/53: ['INJ/USDT', 'ONE/USDT', 'PEPE/USDT']
      real positions: 0/3 | REAL=ON

Three paper longs, three names, none of them in REAL_SYMS. Zero real entries
have been possible all day and nothing said so: 0 `REALBUY`, 0 `ERR`, 0
`entry blocked` lines in 3 days of `logs/trend.log`.

These tests pin the *structure* that makes that silence possible and cheap to
detect. They read source text and the module's constants only; nothing opens
an exchange, a state file or an order path. Mutations that must turn them red:

  * REAL_SYMS made equal to SYMS                → test_real_money_is_still_the_narrow_list
  * paper-only skip removed from the entry loop → test_the_skip_is_still_there
  * the skip made silent again                  → test_a_paper_only_symbol_is_reported

Design rule (see designs/T0.15.md):
  A real-money path that declines to act must say WHICH RULE declined. A filter
  that skips silently is indistinguishable from a market with no setups.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "scripts" / "trend_follower.py"


def _load():
    """Import `scripts/trend_follower.py` without running `main()` or touching a book."""
    sys.path.insert(0, str(REPO / "src"))
    sys.path.insert(0, str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location("trend_follower_t015", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tf():
    return _load()


@pytest.fixture(scope="module")
def text():
    return SRC.read_text(encoding="utf-8")


def test_real_money_is_still_the_narrow_list(tf):
    """REAL_SYMS must remain a strict subset of SYMS — widening it is an owner decision."""
    unexpected = tf.REAL_SYMS - set(tf.SYMS)
    assert not unexpected, f"REAL_SYMS names not in the paper universe: {sorted(unexpected)}"
    assert tf.REAL_SYMS != set(tf.SYMS), (
        "REAL_SYMS has been widened to the whole paper universe — that is a "
        "real-money scope change and belongs in an escalation, not a loop branch"
    )


def test_the_paper_only_skip_is_still_there(text):
    """The entry loop must still refuse a symbol that is not in REAL_SYMS."""
    assert "if s not in REAL_SYMS:" in text, "the real-money symbol gate is gone from the entry loop"


def test_a_paper_only_symbol_is_reported(tf, text):
    """Declining on a paper-only symbol must be visible, not a bare `continue`.

    The measured failure was silence, not the skip: the skip is correct. This
    test runs the reporting helper against the live universe — including the
    exact three names the 2026-11-07 12:10 run printed — and requires the entry
    loop to call it and the run line to say `paper-only`.
    """
    helper = getattr(tf, "paper_only_skipped", None)
    assert helper is not None, (
        "trend_follower has no `paper_only_skipped` helper; the entry loop "
        "cannot report how many candidates the REAL_SYMS gate declined"
    )
    made_up = {"INJ/USDT", "ONE/USDT", "PEPE/USDT", "BTC/USDT"}
    assert helper(["INJ/USDT", "ONE/USDT", "PEPE/USDT", "BTC/USDT"], made_up) == []
    assert helper(["INJ/USDT", "ONE/USDT", "PEPE/USDT", "BTC/USDT"], tf.REAL_SYMS) == \
        ["INJ/USDT", "ONE/USDT", "PEPE/USDT"]
    assert "paper_only_skipped(" in text, "the entry loop never calls the reporting helper"
    assert "paper-only" in text, "the run line does not report paper-only-declined candidates"


def test_no_duplicate_real_universe(tf):
    """A copy that drifts is worse than no copy — unless something compares it.

    This is the universe-copied-many-times class: `TIMEFRAMES` was copied 3+ times
    and one copy silently lost `15m` for weeks. `REAL_SYMS` is likewise typed out
    in `scripts/trend_gate_test.py`, `trend_exit_ab.py` and `trend_trail_test.py`
    (research scripts). Two acceptable states: no copy at all, or a copy whose
    live value equals `trend_follower`'s. A copy that has moved must be reported
    here, not discovered in a research report months later.
    """
    live = tf.REAL_SYMS
    allowed, drifted = [], []
    for p in sorted((REPO / "scripts").glob("*.py")):
        if p.name == "trend_follower.py":
            continue
        src = p.read_text(encoding="utf-8", errors="ignore")
        if "REAL_SYMS = [" not in src and "REAL_SYMS = {" not in src:
            continue
        ns: dict = {}
        block = src.split("REAL_SYMS = ", 1)[1]
        # evaluate just the literal
        depth, lit = 0, ""
        for ch in block:
            lit += ch
            if ch in "[{(":
                depth += 1
            elif ch in "]})":
                depth -= 1
                if depth == 0:
                    break
        try:
            copied = set(eval(lit, ns))  # noqa: S307 — a literal from our own repo
        except Exception as e:  # unparseable copy is drift by definition
            drifted.append(f"{p.name}: unparseable ({type(e).__name__})")
            continue
        (allowed if copied == live else drifted).append(p.name)
    assert not drifted, (
        "REAL_SYMS copies have drifted from scripts/trend_follower.py: "
        f"{drifted} — either delete the copy and import, or keep it equal. "
        f"(equal copies today: {allowed})"
    )
    assert len(allowed) <= 3, f"REAL_SYMS is now copied in {len(allowed)} files: {allowed}"


def test_the_message_says_why_real_did_not_act(tf):
    """`_build_message` must render an INFO event, and must not count it as a change.

    Before this, the only channel for "the real path declined" was ERR or
    nothing. INFO is the third state: not a change, not a failure, a reason.
    """
    title, body = tf._build_message(
        [("INFO", "trend", None, "3 candidate(s) paper-only (not in REAL_SYMS): INJ/USDT, ONE/USDT, PEPE/USDT")],
        n_real=0, real_on=True)
    assert "paper-only" in body and "INJ/USDT" in body, body
    assert "Did not act" in body or "did not act" in body.lower(), body
    assert "0 change" not in title, title


def test_a_real_buy_is_still_counted_as_a_change(tf):
    """The passing control: an INFO change must not swallow the real-change count.

    Both directions, because the first draft of this test only asserted the buy
    case — and the "INFO counted as a change" mutation passed it. A count rule
    needs a mixed event list or it tests nothing.
    """
    buy = [("REALBUY", "BTC/USDT", 80000.0, {"qty": 0.0001, "notional": 8.0, "entry_status": "FILL"})]
    info = [("INFO", "trend", None, "3 candidate(s) paper-only (not in REAL_SYMS): INJ/USDT")]
    title, _ = tf._build_message(buy, n_real=1, real_on=True)
    assert "1 change" in title, title
    title, _ = tf._build_message(buy + info, n_real=1, real_on=True)
    assert "1 change" in title, f"an INFO event was counted as a change: {title}"


def test_the_run_line_carries_the_paper_only_count(tf):
    """The printed run line must name what was declined — this is the line a human reads.

    `main()` is never called: the run line is built from `pos` and `REAL_SYMS`,
    and the helper it uses is the one under test.
    """
    paper = ["INJ/USDT", "ONE/USDT", "PEPE/USDT"]
    assert tf.paper_only_skipped(paper, tf.REAL_SYMS) == paper
    src = SRC.read_text(encoding="utf-8")
    call = 'skipped = paper_only_skipped([c[1] for c in candidates], REAL_SYMS)'
    assert call in src, "the entry loop no longer computes the declined set"
    # the set must reach both readers: the run line and the card
    assert 'if skipped:' in src, "the declined set is computed but nothing reports it"
    line = [ln for ln in src.splitlines() if "paper-only {len(_po)}" in ln]
    assert line, "the run line no longer prints the paper-only count"
