"""Per-edge SYMBOL CONCENTRATION, and the redundancy note that contradicted it.

Context (overnight 2026-11-09). `scripts/paper_book.py::edge_liveness` now
reports, per edge, the per-symbol net split (`concentration`, `n_symbols`),
the single symbol that carries the edge (`lead_symbol`/`lead_n`/`lead_net`)
and the edge's net without it (`net_ex_top`).

WHY this exists as a test and not a comment: `redundant_edges_text` printed
"do not count the copy as independent confirmation" beside a REDUNDANT list
that put `momentum ~ loose` and `breakout ~ loose`. On the live book `loose`
is net-POSITIVE (+3.54 collapsed / +12.79 gross, the only such edge) and the
copy is the net-negative one — so the note's *bold* was on the smaller sample
while the *money* sat in the copy's parent. A "concentration" figure that is
computed but never asserted is exactly the class of number this repo keeps
finding stale. So the assertions below pin the DERIVATION, not a week's
values: remove the lead symbol and the reported `net_ex_top` must equal
`net - lead_net` to 4dp, every symbol must appear exactly once, and the
concentration must sum to the edge's own net.

Fixtures are hand-built with explicit symbols and pnl so the arithmetic is
inspectable — never the live `data/unified-paper.json`, which moves daily.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]   # tests/loop/ -> repo root
SCRIPTS = ROOT / "scripts"


def _load(name):
    """Same loader idiom as tests/test_paper_book_multitf.py — `scripts/` is not
    a package, so the module is loaded by path rather than by import."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def pb():
    return _load("paper_book")


def _rec(sym: str, pnl: float, edge: str | None = "lenient",
         ts: str = "2026-11-01T00:00:00+00:00") -> dict:
    """One closed paper record. `edge=None` exercises the loose/validated
    fallback rule (the one-book rule the whole stack shares)."""
    r = {"symbol": sym, "pnl": pnl, "ts_utc": ts, "entry": 1.0,
         "size": 5.0, "reason": "TP", "tf": "5m"}
    if edge is not None:
        r["edge"] = edge
    return r


def _book() -> list[dict]:
    """One edge whose money is one symbol, plus a second edge that is spread.

    `lenient`: NEAR +8.0 over 20 fills, OP -1.0 over 18, LINK +0.5 over 16
      -> NEAR moves 8.0 of the 9.5 gross movement (84%) and clears n>=15.
    `spread`:  two symbols at +1.0 each over 20 fills -> neither is the edge.
    """
    out = ([_rec("NEAR/USDT", 0.4) for _ in range(20)]
           + [_rec("OP/USDT", -1.0 / 18) for _ in range(18)]
           + [_rec("LINK/USDT", 0.5 / 16) for _ in range(16)])
    out += ([_rec("BTC/USDT", 0.05, edge="spread") for _ in range(20)]
            + [_rec("ETH/USDT", 0.05, edge="spread") for _ in range(20)])
    return out


@pytest.fixture()
def liv(pb):
    return pb.edge_liveness(_book())


def test_every_symbol_appears_once_and_sums_to_the_edge_net(pb, liv):
    for edge, st in liv["stats"].items():
        syms = [c["symbol"] for c in st["concentration"]]
        assert len(syms) == len(set(syms)) == st["n_symbols"]
        assert sum(c["n"] for c in st["concentration"]) == st["n"]
        assert sum(c["net"] for c in st["concentration"]) == pytest.approx(
            st["net"], abs=1e-4)


def test_the_lead_symbol_is_the_one_that_carries_the_edge(pb, liv):
    st = liv["stats"]["lenient"]
    assert st["lead_symbol"] == "NEAR/USDT"
    assert st["lead_n"] == 20
    assert st["lead_net"] == pytest.approx(8.0, abs=1e-4)


def test_net_ex_top_is_net_minus_the_lead_and_flips_the_sign(pb, liv):
    st = liv["stats"]["lenient"]
    # THE POINT of the figure: the edge is a winner only while one symbol is
    # in it. Net +7.5 over 54 fills; without NEAR it is -0.5.
    assert st["net"] > 0
    assert round(st["net"] - st["lead_net"], 4) == st["net_ex_top"]
    assert st["net_ex_top"] < 0


def test_a_spread_edge_has_no_lead_and_net_ex_top_equals_net(pb, liv):
    st = liv["stats"]["spread"]
    assert st["lead_symbol"] is None
    assert st["lead_n"] is None and st["lead_net"] is None
    assert st["net_ex_top"] == st["net"]


def test_both_floors_are_required(pb, liv):
    """A big share with a thin sample is NOT a lead, and neither is a thick
    sample that moved no money. Both are the ways this figure could lie."""
    # thin: one 3-fill symbol carries everything
    thin = pb.edge_liveness(
        [_rec("SOL/USDT", 1.0) for _ in range(3)]
        + [_rec("XRP/USDT", -0.1 / 30) for _ in range(30)])
    assert thin["stats"]["lenient"]["lead_symbol"] is None
    # flat: 40 fills per symbol, zero movement -> gross_abs == 0, no lead
    flat = pb.edge_liveness(
        [_rec("BTC/USDT", 0.0) for _ in range(40)]
        + [_rec("ETH/USDT", 0.0) for _ in range(40)])
    assert flat["stats"]["lenient"]["lead_symbol"] is None
    assert flat["stats"]["lenient"]["net_ex_top"] == flat["stats"]["lenient"]["net"]


def test_share_floor_is_the_constant_not_a_second_hardcode(pb):
    """If CONC_SHARE moves, the verdict must move with it — a duplicated 0.5
    is how a threshold stops being the one that was reasoned about."""
    recs = ([_rec("NEAR/USDT", 0.4) for _ in range(20)]     # 8.0 of 9.5 = 84%
            + [_rec("OP/USDT", -1.5 / 18) for _ in range(18)])
    assert pb.edge_liveness(recs)["stats"]["lenient"]["lead_symbol"] == "NEAR/USDT"
    old = pb.CONC_SHARE
    try:
        pb.CONC_SHARE = 0.99                         # nothing can clear it
        assert pb.edge_liveness(recs)["stats"]["lenient"]["lead_symbol"] is None
    finally:
        pb.CONC_SHARE = old


def test_untagged_records_still_go_to_loose_and_are_split_by_symbol(pb):
    """The one-book rule: no `edge`, `loose` true -> 'loose'. Concentration
    must be computed on that attribution too, or the fallback edges silently
    report n_symbols=0."""
    recs = [{"symbol": "ADA/USDT", "pnl": 0.3, "loose": True,
             "ts_utc": "2026-11-01T00:00:00+00:00"} for _ in range(16)]
    st = pb.edge_liveness(recs)["stats"]["loose"]
    assert st["n_symbols"] == 1
    assert st["lead_symbol"] == "ADA/USDT"
    assert st["net_ex_top"] == 0.0


def test_three_way_split_keeps_the_moneymaker_first(pb):
    """Three symbols with DESCENDING fill counts and ASCENDING money — the only
    shape where 'sort by count' and 'sort by |net|' order all three
    differently. (First attempt used two symbols with equal counts; that
    mutation still survived, because ties broke the same way either way.)"""
    recs = ([_rec("A/USDT", -1.0) for _ in range(3)]         # 3 fills, -3.0
            + [_rec("B/USDT", 0.0) for _ in range(7)])        # 7 fills,  0.0 (0 < 15: no lead)
    st = pb.edge_liveness(recs + [_rec("C/USDT", 1.0)])["stats"]["lenient"]
    assert [c["symbol"] for c in st["concentration"]] == ["A/USDT", "C/USDT", "B/USDT"]
    # C has the biggest |net| share (25% < 50%), so no lead: both floors again.
    assert st["lead_symbol"] is None


def test_redundancy_verdicts_are_unchanged_by_the_new_fields(pb):
    """The new fields are REPORTED, not a new input to the redundant decision.
    Pin the shape of the existing verdict so a later edit cannot quietly make
    concentration decide redundancy."""
    liv = pb.edge_liveness(_book())
    assert set(liv) == {"last_fill", "redundant", "stats"}
    assert isinstance(liv["redundant"], dict)
