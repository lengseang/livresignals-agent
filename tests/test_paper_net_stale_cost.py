"""A recorded `pnl_net` is only NET if it was charged at a live rate.

DEFECT FOUND 2026-10-21
-----------------------
`paper_net.net_of` trusted any recorded `pnl_net`. But `pnl_net` is a NUMBER
frozen at write time, not a formula: a trade closed while `unified_trader`
still held the old 0.03% constant keeps 0.03% forever.

Measured on the live book (`data/unified-paper.json`, 2026-10-21):

    514 records carry `pnl_net`
    226 of them were charged size * 0.0003 = $0.0015 on a $5 position
    the true measured round trip is 0.15452%  (= $0.007726 on $5, 5.2x more)
    the `cost` column held three rates in ONE ledger: 0.0015 / ~0.0077 / ...

Effect: the book read +$3.04 net when the honest figure was +$1.63 — a 46%
overstatement concentrated in the trades the loop is most likely to promote.

WHY IT WAS INVISIBLE
--------------------
Before 2026-10-11 the reader and the writer shared the SAME stale constant, so
"trust the recorded number" and "re-derive from size" agreed. The 2026-10-11
fix corrected the WRITER only. Every record written before that fix stayed
cheap, and the reader kept blessing it — a stale constant outliving the fix
that replaced it, wearing the recorded field's authority.

THE RULE
--------
A cost is data, and data goes stale. Re-derive at read time; trust a recorded
value only when it is consistent with the CURRENT rate. This is SYSTEM_MAP
rule 9 ("never quote a measurement; re-derive it at write time") applied to a
number the book persists instead of one a comment quotes.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import paper_net  # noqa: E402
from paper_net import net_of  # noqa: E402

RT = paper_net.PAPER_COST_RT


def _rec(**kw):
    base = {"pnl": 0.10, "size": 5.0}
    base.update(kw)
    return base


def test_a_stale_cheap_charge_is_recosted_at_the_current_rate():
    """0.03% on a $5 position is not net at a 0.15% round trip."""
    t = _rec(pnl_net=0.10 - 5.0 * 0.0003, cost=5.0 * 0.0003)
    got, costed = net_of(t)
    assert costed is True
    assert abs(got - (0.10 - 5.0 * RT)) < 1e-12, (got, RT)


def test_a_current_charge_is_trusted_exactly():
    """The authoritative writer's own figure must pass through untouched."""
    t = _rec(pnl_net=round(0.10 - 5.0 * RT, 6), cost=round(5.0 * RT, 6))
    got, _ = net_of(t)
    assert got == t["pnl_net"]


def test_an_older_more_expensive_charge_is_not_under_charged():
    """A record charged above today's rate is already conservative — keep it.

    Re-costing DOWN would be the same class of error in the flattering
    direction: fixing a stale number must never make the book look better.
    """
    t = _rec(pnl_net=0.10 - 5.0 * 0.004, cost=5.0 * 0.004)
    got, _ = net_of(t)
    assert got == t["pnl_net"]


def test_missing_pnl_net_still_derives_from_size():
    t = _rec()
    got, costed = net_of(t)
    assert costed is True
    assert abs(got - (0.10 - 5.0 * RT)) < 1e-12


def test_no_size_means_uncosted_gross_never_zero():
    t = {"pnl": 0.25}
    got, costed = net_of(t)
    assert got == 0.25 and costed is False


def test_zero_cost_with_matching_pnl_net_is_a_deliberate_uncharged_book():
    """PAPER_CHARGE_COST=0 writes cost 0.0 AND pnl_net == pnl on purpose.

    That is distinguishable from a stale charge (whose pnl_net != pnl), and
    must not be silently re-costed by the reader — the operator turned
    charging off at the writer, and the reader does not overrule the writer.
    """
    t = _rec(pnl_net=0.10, cost=0.0)
    got, _ = net_of(t)
    assert got == 0.10


def test_sweep_stale_and_current_agree_with_an_independent_reference():
    """Every shape in a small population re-derives to the same number."""
    shapes = [
        _rec(pnl_net=0.10 - 5.0 * 0.0003, cost=5.0 * 0.0003),   # stale cheap
        _rec(pnl_net=round(0.10 - 5.0 * RT, 6), cost=round(5.0 * RT, 6)),
        _rec(pnl_net=0.10 - 5.0 * 0.004, cost=5.0 * 0.004),     # old expensive
        _rec(),                                                  # no pnl_net
        {"pnl": -0.40, "size": 5.0, "pnl_net": -0.4015, "cost": 0.0015},
    ]
    for t in shapes:
        g = t["pnl"]
        cost = t.get("cost")
        if cost is not None and cost >= 5.0 * RT * 0.5:
            want = t["pnl_net"]
        elif cost == 0.0 and t.get("pnl_net") == g:
            want = t["pnl_net"]
        else:
            want = g - 5.0 * RT
        got, _ = net_of(t)
        assert abs(got - want) < 1e-9, (t, got, want)
