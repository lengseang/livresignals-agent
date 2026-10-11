"""The improvement layer must judge edges on NET P&L, not the flattered gross.

Context (2026-10-11): unified_trader began charging the MEASURED exchange
round-trip cost (0.16092%), 5.4x the stale 0.03% it had used. The cost basis
was propagated to unified_trader and self_improve, but TWO decision makers were
left reading raw gross `pnl`:

  * scripts/improvement_loop.py — the per-edge promote/demote verdicts, whose
    "accuracy-improvement core" is the whole point of the loop.
  * scripts/night_tuner.py — `_stats()` summed gross into a field it LABELLED
    `net`, and that figure feeds a real-money stop-loss adjustment.

Measured on the real book this is not cosmetic: at gross the improvement report
read "+$18.73 / PF 1.23 / WR 47.3%"; at net it is "-$2.69 / PF 0.97 / WR 42.3%",
and the `validated` edge flips sign (+$4.14 -> -$2.17). An edge whose true edge
is thinner than the fee was being promoted.

These tests pin the basis so it cannot silently revert to gross.
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


def _analyze_on(il, trades):
    """Run improvement_loop.analyze() against an in-memory paper book."""
    orig = il._load_json

    def fake(path, default):
        if path.name == "unified-paper.json":
            return {"closed": trades, "open": []}
        return {"closed": [], "open": []}

    il._load_json = fake
    try:
        return il.analyze()
    finally:
        il._load_json = orig


# --------------------------------------------------------------- improvement_loop
def test_improvement_loop_stats_report_net_not_gross():
    il = _load("improvement_loop")
    # 3 trades on a $5 size each, measured RT ~0.0016 -> ~0.008 cost each.
    #   net: +0.992, -0.003, -0.508 -> the middle trade is a GROSS WIN, NET LOSS
    recs = [
        {"pnl": 1.00, "size": 5.0, "symbol": "A/USDT", "edge": "e"},
        {"pnl": 0.005, "size": 5.0, "symbol": "B/USDT", "edge": "e"},
        {"pnl": -0.50, "size": 5.0, "symbol": "C/USDT", "edge": "e"},
    ]
    s = _analyze_on(il, recs)["paper"]
    assert s["gross_pnl"] > 0
    assert s["total_pnl"] < s["gross_pnl"], "costs must be charged on net"
    assert s["win_rate_gross"] == 66.7, "2 of 3 are gross-positive"
    assert s["win_rate"] < s["win_rate_gross"], \
        "the marginal gross winner must drop out on net"


def test_improvement_loop_edge_rows_carry_net_and_gross():
    il = _load("improvement_loop")
    recs = [{"pnl": 0.005, "size": 5.0, "symbol": "A/USDT", "edge": "thin"}
            for _ in range(2)]
    rep = _analyze_on(il, recs)
    row = next(r for r in rep["by_edge"] if r["edge"] == "thin")
    assert row["gross_pnl"] > 0, "gross is positive (0.005 x 2)"
    assert row["net_pnl"] < 0, \
        "net must flip negative once the measured round trip is charged"
    # the report must DECLARE its basis, so a reader cannot mistake it
    assert rep["pnl_basis"] == "net"


def test_improvement_loop_flags_net_negative_thin_edge_as_decaying():
    il = _load("improvement_loop")
    recs = [{"pnl": 0.005, "size": 5.0, "symbol": "A/USDT", "edge": "thin"}
            for _ in range(20)]
    rep = _analyze_on(il, recs)
    kinds = {s["type"] for s in rep["suggestions"]}
    # a gross-only book never decayed here; on net it must be flagged
    assert "edge_decay" in kinds, \
        f"thin gross-positive/net-negative edge not flagged: {rep['suggestions']}"


# ------------------------------------------------------------------- night_tuner
def test_night_tuner_stats_net_flag_charges_cost():
    nt = _load("night_tuner")
    recs = [{"pnl": 0.005, "size": 5.0}, {"pnl": 0.005, "size": 5.0}]
    gross = nt._stats(recs)
    net = nt._stats(recs, net=True)
    assert gross["net"] > 0, "without the flag it is the gross sum"
    assert net["net"] < 0, "net=True must charge the measured round trip"
    assert net["net"] < gross["net"]


def test_night_tuner_live_rows_are_not_double_costed():
    """Live fills already include the real fee; net=True is paper-only."""
    nt = _load("night_tuner")
    live = [{"pnl": 0.10}, {"pnl": -0.05}]  # no `size`: a real fill
    assert nt._stats(live)["net"] == 0.05, \
        "live P&L must be taken at face value"
    # even if handed net=True, an unrecoverable cost falls back to gross
    st2 = nt._stats(live, net=True)
    assert st2["net"] == 0.05
    assert st2.get("uncosted") == 2


def test_night_tuner_paper_windows_are_net():
    src = (SCRIPTS / "night_tuner.py").read_text(encoding="utf-8")
    assert "_stats(p24, net=True)" in src, "paper_24h must be net"
    assert "_stats(p72, net=True)" in src, "paper_72h must be net"
    assert "_stats(rows, net=True)" in src, "per-edge 72h must be net"
    assert "_stats(l24, net=True)" not in src, "live fills must not be re-costed"
