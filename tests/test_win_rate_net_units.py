"""A verdict that says "net" must count its wins on NET — including the win RATE.

Found 2026-10-18 on the real book (`reports/improvement-report.json`,
2026-10-17T09:30Z, re-measured 2026-10-18):

    edge         n     win_rate   net       gross
    validated  878       47.5    -3.495    +2.958   -> "DECAYING (net): 47.5% WR"
    loose     1376       48.8    +4.470   +14.258   -> spared (the control)

Both figures in that sentence claim to be NET, but only one of them is. The
per-edge table counted its wins on GROSS `pnl` ("if (t.get('pnl', 0) or 0) > 0:
wins += 1") while the money beside it was net of the measured round trip, so a
trade that the cost turns from a small winner into a small loser was counted as
a WIN in the "net" verdict. `_stats()` for the whole book already counts net wins
(`netwins`); the per-edge and per-regime loops never got that fix, and the same
mixed units leaked into night_tuner's `net=True` branch and self_improve's
`stats_net_of`.

This is the fourth instance of one class (gross->net on the *basis*, then on the
*disable predicate*, then on the *promote/demote rule*, now on the *win count*).
The repo already has the canonical reader — `paper_net.net_of` — and the writer's
own `paper_net.summarise`, which counts `wr_net` correctly; the tests below pin
every consumer to ONE basis instead of re-deriving it locally.

Direction of the bug matters: counting gross wins INFLATES the win rate of a
net-losing edge (validated reads 47.5% instead of 43.7% on the real book), so
the flattered metric ran the SAME way as the flattered P&L it sat next to.
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


# A MARGINAL gross winner: +0.005 on a $5 size. The measured round trip is
# ~0.154% = $0.0077 per $5 trade, so every one of these is a small NET LOSS.
MARGINAL = 0.005


def _marginal_gross_winners(edge="thin", n=20, symbol="BTC/USDT"):
    return [{"pnl": MARGINAL, "size": 5.0, "symbol": symbol, "edge": edge}
            for _ in range(n)]


# --------------------------------------------------------------- improvement_loop
def test_per_edge_win_rate_counts_net_wins_not_gross_wins():
    il = _load("improvement_loop")
    rep = _analyze_on(il, _marginal_gross_winners())
    row = next(r for r in rep["by_edge"] if r["edge"] == "thin")
    assert row["n"] == 20
    assert row["net_pnl"] < 0, "the cost must make these losers on net"
    assert row["win_rate"] == 0.0, (
        "a verdict labelled NET must not count a gross winner that the fee "
        f"turned into a loser: win_rate={row['win_rate']}")
    # gross is retained for the reader, but only under a gross label
    assert row["win_rate_gross"] == 100.0


def test_per_edge_pays_its_way_out_of_the_win_rate_floor():
    """A NET-POSITIVE edge whose NET win rate is under the 45% floor must NOT be
    told to stand down. The floor is a heuristic about fee drag; a large sample
    that pays after the fee refutes it. Found on the real book 2026-10-18: after
    the net-WR fix, 'loose' read 44.1% WR and +4.47 net — the only net-positive
    edge in the book — and the old `win_rate < 45` branch told the owner to
    stand it down or tighten it. (Under gross units this edge read 100% WR and
    was spared; the two units disagreed about the same edge, which is the bug.)"""
    il = _load("improvement_loop")
    recs = _marginal_gross_winners(n=20)
    # 5 genuine net winners (+1.00 each) + 20 marginal gross winners
    # -> n=25, net P&L = 5*0.9923 - 20*0.0027 = +4.91 (POSITIVE),
    #    net WR 20%, gross WR 100%.
    recs += [{"pnl": 1.00, "size": 5.0, "symbol": "BTC/USDT", "edge": "thin"}
             for _ in range(5)]
    rep = _analyze_on(il, recs)
    row = next(r for r in rep["by_edge"] if r["edge"] == "thin")
    assert row["n"] == 25, row
    assert row["net_pnl"] > 0, row
    assert row["win_rate"] == 20.0, row
    assert row["win_rate_gross"] == 100.0, row
    decay = [s for s in rep["suggestions"]
             if s["type"] == "edge_decay" and "thin" in s["idea"]]
    assert decay, f"low-net-WR edge drew NO note at all: {rep['suggestions']}"
    idea = decay[0]["idea"]
    assert "net-NEGATIVE" not in idea, (
        f"a net-POSITIVE edge must not be described as net-negative: {idea}")
    assert "stand it down" not in idea, (
        f"a net-POSITIVE edge must not be told to stand down: {idea}")
    assert decay[0]["priority"] == 3, (
        f"a paying edge is advisory (p3), not an action (p2): {decay[0]}")
    assert "20.0% WR" in idea, decay[0]


def test_net_negative_low_wr_edge_keeps_the_actionable_verdict():
    """The other route must not be softened by the guard: a net-NEGATIVE edge
    under the floor still gets the p2 'stand it down' verdict."""
    il = _load("improvement_loop")
    rep = _analyze_on(il, _marginal_gross_winners())   # all net losers, 0% net WR
    row = next(r for r in rep["by_edge"] if r["edge"] == "thin")
    assert row["net_pnl"] < 0
    decay = [s for s in rep["suggestions"]
             if s["type"] == "edge_decay" and "thin" in s["idea"]]
    assert decay, rep["suggestions"]
    assert decay[0]["priority"] == 2, decay[0]
    assert "net-NEGATIVE" in decay[0]["idea"], decay[0]
    assert "stand it down" in decay[0]["idea"], decay[0]


def test_per_regime_win_rate_counts_net_wins_not_gross_wins():
    il = _load("improvement_loop")
    recs = [dict(r, regime="RANGING") for r in _marginal_gross_winners()]
    rep = _analyze_on(il, recs)
    row = next(r for r in rep["by_regime"] if r["regime"] == "RANGING")
    assert row["avg_pnl"] < 0
    assert row["win_rate"] == 0.0, (
        f"per-regime win_rate must be net-of-cost like its avg_pnl: {row}")
    assert row["win_rate_gross"] == 100.0


# ------------------------------------------------------------------- night_tuner
def test_night_tuner_net_stats_count_net_wins():
    nt = _load("night_tuner")
    rows = [{"pnl": MARGINAL, "size": 5.0} for _ in range(4)]
    net = nt._stats(rows, net=True)
    assert net["net"] < 0
    assert net["win_rate"] == 0.0, (
        f"net=True must not report a gross win rate: {net}")
    # the gross branch keeps its gross win rate — the number is not removed,
    # it is labelled
    assert nt._stats(rows)["win_rate"] == 100.0


# ------------------------------------------------------------------ self_improve
def test_self_improve_net_stats_expose_gross_win_rate():
    si = _load("self_improve")
    rows = [{"pnl": MARGINAL, "size": 5.0} for _ in range(4)]
    st = si.stats_net_of(rows)
    assert st["pnl"] < 0
    assert st["wr"] == 0.0, st
    assert st.get("wr_gross") == 100.0, (
        "the gross figure must be reported as gross, not substituted for net")


# ------------------------------------------------------------------- paper_net
def test_paper_net_summarise_counts_gross_wins_for_its_gross_rate():
    """The canonical reader already separates the two; pin it so a 'fix' in the
    consumers cannot be back-ported into it by accident."""
    pn = _load("paper_net")
    assert pn.PAPER_COST_RT * 5.0 > MARGINAL, "fixture must be net-negative"
    s = pn.summarise([{"pnl": MARGINAL, "size": 5.0} for _ in range(4)])
    assert s["wr_net"] == 0.0
    assert s["wr_gross"] == 100.0
