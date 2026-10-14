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

The fix is a last-edge FLOOR in `_plan_disables`: a disable is only safe while
something REMAINS to carry the book. If applying the rule would leave zero
enabled edges, the strongest candidate is kept and the rest still go.

These tests drive `_plan_disables` (the real decision function) so a mutation
that neuters the floor fails them — pinning behaviour, not source strings.
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
    """Trades that trip the disable predicate at NET: negative net P&L, WR 0%."""
    return [{"pnl": per_trade_pnl, "size": size, "symbol": "X/USDT",
             "edge": edge} for _ in range(n)]


def test_should_disable_is_the_net_predicate():
    """The extracted predicate reads NET stats, and a tiny sample never trips."""
    si = _load("self_improve")
    assert si._should_disable(si.stats_net_of(_loser("thin"))) is True
    assert si._should_disable(si.stats_net_of(_loser("thin", n=5))) is False


def test_predicate_alone_would_empty_a_book_of_losers():
    """Document WHY the floor is needed: every edge trips the raw rule."""
    si = _load("self_improve")
    edges = ["validated", "loose", "momentum", "rsi2"]
    flagged = [e for e in edges if si._should_disable(si.stats_net_of(_loser(e)))]
    assert flagged == edges, "the raw rule empties the whole book — hence the floor"


def test_floor_keeps_exactly_one_when_all_would_be_disabled():
    """All candidates flagged, nothing else enabled -> exactly one survives."""
    si = _load("self_improve")
    by_edge = {e: _loser(e) for e in ("validated", "loose", "momentum")}
    to_disable, survivor = si._plan_disables(by_edge, already_disabled=[])
    assert survivor is not None, "the floor MUST fire when it would empty the book"
    assert survivor in by_edge
    assert survivor not in to_disable, "the survivor must be held back"
    assert len(to_disable) == 2, "the other two still go"
    # book not emptied: survivor + one other disabled edge leaves it tradeable
    assert {survivor} | set(to_disable) == set(by_edge)


def test_floor_keeps_the_strongest_candidate():
    """The survivor is the least-bad edge (highest net, then n)."""
    si = _load("self_improve")
    by_edge = {
        "worst": _loser("worst", per_trade_pnl=-0.10),   # very negative net
        "best": _loser("best", per_trade_pnl=-0.001),    # near break-even net
    }
    to_disable, survivor = si._plan_disables(by_edge, already_disabled=[])
    assert survivor == "best", "must keep the edge closest to viable"
    assert to_disable == ["worst"]


def test_floor_not_triggered_when_another_edge_stays_enabled():
    """A healthy edge remains -> a genuine loser is disabled normally."""
    si = _load("self_improve")
    good = [{"pnl": 0.5, "size": 5.0, "symbol": "G/USDT", "edge": "good"}
            for _ in range(20)]
    by_edge = {"good": good, "validated": _loser("validated")}
    to_disable, survivor = si._plan_disables(by_edge, already_disabled=[])
    assert to_disable == ["validated"], "the loser is disabled"
    assert survivor is None, "the floor must NOT fire while 'good' remains"


def test_already_disabled_edges_do_not_count_as_openable():
    """An edge already on the list cannot 'carry the book' — the floor must
    still fire when the only non-flagged edge is something already disabled."""
    si = _load("self_improve")
    by_edge = {"dead": _loser("dead"), "last": _loser("last", per_trade_pnl=-0.01)}
    # 'dead' is already disabled, so it cannot carry the book; 'last' is the
    # only openable edge and must be protected.
    to_disable, survivor = si._plan_disables(by_edge, already_disabled=["dead"])
    assert survivor == "last"
    assert to_disable == []


def test_main_uses_the_shared_planner_and_reports_loudly():
    """Pin the wiring: main() must call _plan_disables, and a frozen book must
    be surfaced as a warning on the card, not buried in a log."""
    src = (SCRIPTS / "self_improve.py").read_text(encoding="utf-8")
    assert "_plan_disables(by_edge, state[\"disabled\"])" in src, \
        "main() must use the shared, tested planner"
    assert "REFUSED to disable the last open edge" in src, \
        "the refusal must be reported (loud, on the card), not silent"
    assert "EVERY edge is disabled" in src, \
        "a frozen book must be surfaced as a warning, not buried in a log"
