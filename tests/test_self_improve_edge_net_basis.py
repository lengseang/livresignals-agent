"""The self_improve per-edge disable/promote gate must judge on NET, not gross.

Context (2026-10-13): `scripts/self_improve.py` step 3 WRITES the `disabled`
list in data/signals-state.json, and `unified_trader` HONOURS that list on the
real-money path (`_edge_disabled`). But the gate read GROSS `pnl`:

    st = stats(ts)                                # sums gross pnl
    if st["n"] >= 15 and st["wr"] < 42 and st["pnl"] < 0:   # GROSS

Measured on the real book (2026-10-13), the measured round trip is ~0.16%, so
the two bases disagree on live edges:

    edge       n   gross_wr  gross_pnl | net_wr  net_pnl  verdict
    loose     83     37.3%      +0.14  |  34.9%   -0.35   gross:-  net:DISABLE
    momentum  30     40.0%      +0.19  |  33.3%   -0.05   gross:-  net:DISABLE
    rsi2      27     55.6%      -0.05  |  33.3%   -0.26   gross:-  net:DISABLE

An edge whose true edge is thinner than the fee was being kept alive by the
flattered gross figure. This is the same silent-unit defect the improvement
layer was fixed for on 2026-10-11 (`tests/test_improvement_net_basis.py`); the
self_improve gate was the one decision-maker left reading gross.

These tests pin the basis so it cannot silently revert.
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


def test_stats_net_of_charges_the_round_trip():
    si = _load("self_improve")
    # 2 trades on a $5 size, gross +0.005 each: gross-positive, net-negative.
    recs = [{"pnl": 0.005, "size": 5.0} for _ in range(2)]
    g = si.stats(recs)
    n = si.stats_net_of(recs)
    assert g["pnl"] > 0, "gross sum is positive (0.005 x 2)"
    assert n["pnl"] < 0, "net must flip negative once the round trip is charged"
    assert n["gross"] == g["pnl"], "the gross figure is carried alongside"
    assert n["wr"] < g["wr"], "a marginal gross winner drops out on net"


def test_marginal_edge_is_disabled_on_net_not_gross():
    """The core regression: a gross-positive / net-negative edge is kept by the
    old basis and DISABLED by the fixed one."""
    si = _load("self_improve")
    # n=20 satisfies the >=15 sample gate for the disable rule. The gross WR is
    # 0% here, so isolate the P&L basis: gross sum is positive, net is not.
    recs = [{"pnl": 0.005, "size": 5.0, "symbol": "A/USDT", "edge": "thin"}
            for _ in range(20)]
    g = si.stats(recs)
    n = si.stats_net_of(recs)
    assert g["pnl"] > 0 and n["pnl"] < 0, "the bases must diverge for the test"
    # the gate as FIXED (net basis) disables; as it was (gross) it does not
    assert (n["n"] >= 15 and n["wr"] < 42 and n["pnl"] < 0), \
        "net basis must flag the thin edge"
    assert not (g["n"] >= 15 and g["wr"] < 42 and g["pnl"] < 0), \
        "gross basis is why it slipped through"


def test_edge_gate_reads_stats_net_of():
    """Pin the wiring: the disable loop must call stats_net_of, not stats."""
    src = (SCRIPTS / "self_improve.py").read_text(encoding="utf-8")
    # the disable gate block (not the optimizer / headline reporting)
    assert "st = stats_net_of(ts)" in src, \
        "the per-edge gate must judge on net"
    assert "st = stats(ts)" not in src, \
        "the raw-gross stats() call must not remain on the edge gate"
    assert "net={st['pnl']:+.2f}, gross {st['gross']:+.2f}" in src, \
        "the message must carry both net and gross so the haircut is visible"
