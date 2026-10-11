"""Tests for scripts/paper_net.py — the honest (net-of-cost) paper P&L reader.

Context (2026-10-06): unified_trader began charging costs and recording
`pnl_net` beside gross `pnl`. The self-improve card kept quoting gross. These
tests pin the reader that fixes that, and — critically — pin the behaviour on a
record with NO `pnl_net` and NO `size`, which must be reported as UNCOSTED, not
silently averaged in as zero.
"""
import importlib.util
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
SCRIPTS = BASE / "scripts"


def _load():
    spec = importlib.util.spec_from_file_location(
        "paper_net_under_test", SCRIPTS / "paper_net.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_recorded_pnl_net_wins():
    pn = _load()
    v, costed = pn.net_of({"pnl": 1.0, "pnl_net": 0.99, "size": 5.0})
    assert costed is True
    assert v == 0.99, "the writer's pnl_net must be authoritative"


def test_missing_pnl_net_is_derived_like_the_writer():
    pn = _load()
    v, costed = pn.net_of({"pnl": 1.0, "size": 5.0})
    assert costed is True
    assert abs(v - (1.0 - 5.0 * pn.PAPER_COST_RT)) < 1e-12


def test_no_size_no_pnl_net_is_uncosted_not_zero():
    pn = _load()
    v, costed = pn.net_of({"pnl": 2.5})
    assert v == 2.5, "must not invent a cost it cannot compute"
    assert costed is False, "must be reported as uncosted, never as a net zero"


def test_missing_pnl_yields_nothing():
    pn = _load()
    assert pn.net_of({"size": 5.0}) == (0.0, False)


def test_summarise_counts_net_wins_not_gross_wins():
    pn = _load()
    # 1.0 gross with a $5 size costs 0.0015 -> still a win
    # 0.01 gross with a $5 size costs 0.0015 -> net +0.0085, still a win
    # but a 0.001 gross with a $5 size -> net -0.0005: a GROSS WIN that is a NET LOSS
    recs = [
        {"pnl": 1.0, "size": 5.0},
        {"pnl": 0.01, "size": 5.0},
        {"pnl": 0.001, "size": 5.0},
    ]
    s = pn.summarise(recs)
    assert s["n"] == 3
    assert s["wr_gross"] == 100.0
    assert s["wr_net"] < 100.0, "the marginal gross winner must drop out of net"
    assert abs(s["net_pnl"] - (1.0 + 0.01 + 0.001 - 3 * 5.0 * pn.PAPER_COST_RT)) < 1e-9
    assert s["uncosted"] == 0


def test_summarise_empty_is_safe():
    pn = _load()
    s = pn.summarise([])
    assert s["n"] == 0 and s["net_pnl"] == 0.0 and s["uncosted"] == 0


def test_per_symbol_net_groups_and_signs():
    pn = _load()
    recs = [
        {"symbol": "AAA/USDT", "pnl": 2.0, "size": 5.0},
        {"symbol": "AAA/USDT", "pnl": -1.0, "size": 5.0},
        {"symbol": "BBB/USDT", "pnl": 0.001, "size": 5.0},
    ]
    out = pn.per_symbol_net(recs)
    assert out["AAA/USDT"]["n"] == 2
    assert out["AAA/USDT"]["net"] > 0
    assert out["BBB/USDT"]["n"] == 1
    assert out["BBB/USDT"]["net"] < 0, "tiny gross win is a net loss after cost"


def test_cost_rt_matches_unified_trader_default():
    """The reader's cost must not silently drift from the writer's.

    Since 2026-10-11 both resolve the SAME way: env override -> measured
    `data/fee-audit.json` round trip -> the 0.0003 fallback. Asserting a literal
    is no longer enough — the two must AGREE on whichever basis is active, and
    the active basis must be the measured one when the audit exists.
    """
    pn = _load()
    src = (SCRIPTS / "unified_trader.py").read_text(encoding="utf-8")
    # both read the measured value from the same file...
    assert "measured_round_trip_pct" in src, \
        "unified_trader no longer reads the measured round trip"
    assert "measured_round_trip_pct" in (
        SCRIPTS / "paper_net.py").read_text(encoding="utf-8"), \
        "paper_net no longer reads the measured round trip"

    # ...and resolve to the SAME number in this checkout.
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("ut_drift", SCRIPTS / "unified_trader.py")
    ut = _ilu.module_from_spec(spec)
    sys.modules["ut_drift"] = ut
    spec.loader.exec_module(ut)
    assert abs(pn.PAPER_COST_RT - ut.PAPER_COST_RT) < 1e-12, \
        f"reader {pn.PAPER_COST_RT} != writer {ut.PAPER_COST_RT}"

    audit = json.loads((BASE / "data" / "fee-audit.json").read_text())
    pct = audit.get("totals", {}).get("measured_round_trip_pct")
    if pct:
        assert abs(pn.PAPER_COST_RT - pct / 100.0) < 1e-9
        assert pn.PAPER_COST_RT != 0.0003, "stale constant is still in force"
