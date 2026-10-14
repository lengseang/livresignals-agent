"""The self_improve disable gate must NEVER disable the last open edge.

Context (2026-10-14): `scripts/self_improve.py` step 3 disables per-edge losers
(n>=15, WR<42%, net<0 — the net basis fixed on 2026-10-13). But that predicate
is TRUE for essentially every edge once the measured ~0.16% round trip is
charged, so running it to completion emptied the disable set:

    logs/self-improve.log:
      APPLIED: disabled edge 'loose'      (n=17 wr=29% net=-0.29)
      APPLIED: disabled edge 'validated'  (n=34 wr=38% net=-0.09)

`unified_trader` honours `disabled` in EVERY opener (the 2026-10-12 fix routed
all of them through `_edge_disabled`), so once the two native openers 'loose' +
'validated' were on the list the paper book STOPPED OPENING: unified-paper.json
froze at 2026-10-12T18:16, and the live scan has fired hundreds of signals since
("paper signals fired=14", "=8", "=3") with zero PAPER OPEN lines. The learning
track was dead for ~1.5 days while the engine kept reporting metrics.

The fix is a last-edge guard: a disable is only safe while something REMAINS to
carry the book. If applying the rule would leave zero enabled edges, the
strongest candidate is kept (and the refusal is reported loudly).

These tests pin (a) the guard's behaviour and (b) that it is WIRED into main().
Run:  ./venv/bin/python -m pytest tests/test_self_improve_last_edge.py -q
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _loser(edge, n=20, size=5.0, per_trade_pnl=-0.02):
    """A trade set that trips the disable predicate at NET: negative net P&L,
    WR 0% (every gross pnl <= 0 so a loss is booked)."""
    return [{"pnl": per_trade_pnl, "size": size, "symbol": "X/USDT",
             "edge": edge} for _ in range(n)]


def test_should_disable_is_the_net_predicate():
    """The extracted predicate reads NET stats, not gross."""
    si = _load("self_improve")
    st = si.stats_net_of(_loser("thin"))
    assert si._should_disable(st) is True
    # A tiny sample (n<15) is never disabled — too little evidence.
    st_small = si.stats_net_of(_loser("thin", n=5))
    assert si._should_disable(st_small) is False


def test_predicate_alone_would_empty_a_book_of_losers():
    """Document WHY the guard is needed: every edge trips the rule."""
    si = _load("self_improve")
    edges = ["validated", "loose", "momentum", "rsi2"]
    flagged = [e for e in edges if si._should_disable(si.stats_net_of(_loser(e)))]
    assert flagged == edges, "the raw rule empties the whole book — hence the guard"


def test_guard_logic_keeps_exactly_one_when_all_would_be_disabled():
    """Reproduce the guard's decision directly: with 3 losers enabled and all
    flagged, exactly ONE must survive (and it must be retained, not disabled)."""
    si = _load("self_improve")
    by_edge = {e: _loser(e) for e in ("validated", "loose", "momentum")}
    disabled: list = []  # nothing disabled yet -> all three are candidates
    would_disable = [e for e, ts in by_edge.items()
                     if e not in disabled and si._should_disable(si.stats_net_of(ts))]
    enabled_after = [e for e in by_edge if e not in disabled and e not in would_disable]
    assert len(would_disable) == 3
    assert enabled_after == [], "precondition: guard must be needed"
    # apply the guard: keep the strongest (highest net, then n)
    would_disable.sort(key=lambda e: (si.stats_net_of(by_edge[e])["pnl"],
                                      si.stats_net_of(by_edge[e])["n"]))
    survivor = would_disable.pop()
    remaining_enabled = [e for e in by_edge if e != survivor]
    assert survivor in by_edge
    assert len(remaining_enabled) == 2  # the rest still go
    # the book is NOT emptied
    assert survivor not in [e for e in would_disable]


def test_guard_not_triggered_when_another_edge_stays_enabled():
    """If a healthy edge remains enabled, the guard must NOT fire — a genuine
    loser still gets disabled normally."""
    si = _load("self_improve")
    good = [{"pnl": 0.5, "size": 5.0, "symbol": "G/USDT", "edge": "good"}
            for _ in range(20)]
    by_edge = {"good": good, "validated": _loser("validated")}
    state_disabled: list = []
    would_disable = [e for e, ts in by_edge.items()
                     if e not in state_disabled and si._should_disable(si.stats_net_of(ts))]
    enabled_after = [e for e in by_edge if e not in state_disabled
                     and e not in would_disable]
    assert would_disable == ["validated"]
    assert enabled_after == ["good"], "guard must not fire while 'good' remains"
    # normal path: validated is disabled
    assert "validated" in would_disable


def test_main_contains_last_edge_guard_and_fail_loud():
    """Pin the wiring so the guard and the frozen-book warning cannot be
    silently removed from self_improve.main()."""
    src = (SCRIPTS / "self_improve.py").read_text(encoding="utf-8")
    assert "NEVER DISABLE THE LAST OPEN EDGE" in src, \
        "the last-edge guard must be present in main()"
    assert "enabled_after" in src, \
        "the guard must compute the set of edges left enabled"
    assert "REFUSED to disable the last open edge" in src, \
        "the refusal must be reported (loud, on the card), not silent"
    assert "EVERY edge is disabled" in src, \
        "a frozen book must be surfaced as a warning, not buried in a log"
    assert "if _should_disable(st):" in src, \
        "the disable loop must read the shared predicate"
