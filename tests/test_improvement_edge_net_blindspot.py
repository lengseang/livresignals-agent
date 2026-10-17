"""The improvement loop's per-edge verdict must not be blind to NET P&L.

Found 2026-10-17 on the real book (`reports/improvement-report.json`,
2026-10-17T01:01Z):

    edge             n     wr       net      gross
    loose         1376   48.8    +4.376   +14.258
    validated      843   48.4    -2.808    +3.428     <-- largest loss, INVISIBLE

`improvement_loop._suggest` carried a per-edge PROMOTE rule (WR>=58 & avg>0)
and a per-edge DEMOTE rule (WR<45). Both key on WIN RATE. `validated` sits at
48.4% WR — between the two thresholds — so it drew NO suggestion at all, even
though it loses $2.81 net over 843 trades: the LARGEST single loss in the book,
larger than every edge that WAS flagged (supertrend -1.85, pullback -1.11,
bollinger -1.04, ema-cross -0.83, breakout -0.44) COMBINED.

Win rate and P&L are not the same question. An edge can win half its trades and
still bleed money if the wins are small and the losses are large — which is
exactly what a capitulation-dip opener does when the dip keeps going. A verdict
that only reads the win rate cannot see a loss that is spread across many
small trades, and that is the loss most likely to hide.

The demote rule therefore judges NET P&L as well as win rate: an edge with a
real sample that is NET-NEGATIVE is declining regardless of how often it wins.
The reverse is deliberately NOT changed: a net-POSITIVE edge below the promote
bar is not promoted on P&L alone (the promote bar stays strict, per the repo's
"promote slowly" rule), it is only spared the decay verdict.
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


def _row(edge, n, win_rate, net_pnl, avg_pnl):
    return {"edge": edge, "n": n, "win_rate": win_rate, "avg_pnl": avg_pnl,
            "net_pnl": net_pnl, "gross_pnl": net_pnl, "symbols": ["BTC/USDT"]}


def test_net_negative_edge_above_the_wr_floor_is_still_flagged():
    """THE REAL-WORLD CASE (2026-10-17). `validated` loses $2.81 net at 48.4%
    WR — inside the 45-58% blind spot, so the loop said nothing about the
    book's largest loss. A net-negative edge with a real sample must be
    flagged as declining regardless of win rate."""
    edges = [_row("validated", 843, 48.4, -2.8078, -0.0033)]
    sugs = il._suggest({"n": 2491, "win_rate": 42.7, "profit_factor": 0.95},
                       None, [], edges, None)
    flagged = [s for s in sugs if s["type"] == "edge_decay"
               and "validated" in s["idea"]]
    assert flagged, f"net-negative edge drew no verdict: {sugs}"
    assert "net" in flagged[0]["idea"].lower(), flagged[0]["idea"]


def test_net_positive_edge_inside_the_blind_spot_is_not_flagged():
    """The guard must not cry wolf. `loose` (+$4.38 net at 48.8% WR) sits in the
    same win-rate band but EARNS; it must NOT be called decaying for a low win
    rate. The promote bar stays strict (WR>=58), so it earns no promote either
    — it is simply left alone."""
    edges = [_row("loose", 1376, 48.8, 4.3763, 0.0032)]
    sugs = il._suggest({"n": 2491, "win_rate": 42.7, "profit_factor": 0.95},
                       None, [], edges, None)
    kinds_for_loose = [s for s in sugs if "loose" in s["idea"]]
    assert not any(s["type"] == "edge_decay" for s in kinds_for_loose), \
        f"a net-POSITIVE edge was called decaying: {kinds_for_loose}"
    assert not any(s["type"] == "edge_promote" for s in kinds_for_loose), \
        f"the promote bar must stay strict: {kinds_for_loose}"


def test_thin_sample_is_not_flagged_on_net_alone():
    """A net-negative edge below the n>=10 floor has no verdict — a 3-trade
    loss is noise, not decay ("a 1-trade window must not decide anything")."""
    edges = [_row("tiny", 4, 50.0, -0.9, -0.225)]
    sugs = il._suggest({"n": 2491, "win_rate": 42.7, "profit_factor": 0.95},
                       None, [], edges, None)
    assert not any("tiny" in s["idea"] for s in sugs), sugs


def test_high_wr_net_loser_is_flagged_and_not_promoted():
    """An edge that wins often but loses money is the worst kind — it must be
    flagged, and must never be reported as PROVEN."""
    edges = [_row("trap", 40, 62.0, -0.5, -0.0125)]
    sugs = il._suggest({"n": 2491, "win_rate": 42.7, "profit_factor": 0.95},
                       None, [], edges, None)
    kinds = {s["type"] for s in sugs if "trap" in s["idea"]}
    assert "edge_decay" in kinds, sugs
    assert "edge_promote" not in kinds, sugs
