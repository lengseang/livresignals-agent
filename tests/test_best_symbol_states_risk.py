"""A "best symbol" line must state the RISK, not just the leader's mean.

THE DEFECT (found 2026-11-07)
-----------------------------
`self_improve` printed, every run:

    paper best symbol: NEAR/USDT n=504 net=+9.333 (gross +9.972)
    paper net-LOSING symbols (n>=20): 11 — ETH/USDT -0.03, ...

Read together, the card said "11 of 12 symbols lose and one pays", which is the
single most important fact about the paper book — but neither line could say
it. "best symbol" reads as a property of the BOOK ("this is what we are good
at"), when the measured fact is a concentration: NEAR was +9.33 while the other
eleven summed to -2.55. Delete one name and the section inverts.

This is SYSTEM_MAP rule 25(c) — "ONE RECORD THAT FLIPS THE SIGN IS NOT A TRACK
RECORD — print the remove-top-record figure beside the headline" — for the
third time (public ledger demo-0004-NEARUSDT, the loop's stop tail, the real
book's trend log). The line must now carry the leader's SHARE of the book's
net, derived at write time (rule 9), and must not manufacture a percentage on a
book whose net is <= 0 (rule 15: never quote a ratio the population does not
support).

The rendering lives in `_symbol_lines()` so it can be tested without running
the whole self_improve pipeline (rule 10: a monitor publishes only from its
entrypoint).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    sys.path.insert(0, str(ROOT / "scripts"))
    import importlib
    if "self_improve" in sys.modules:
        return importlib.reload(sys.modules["self_improve"])
    import self_improve
    return self_improve


def _trade(sym, pnl, net=None):
    t = {"symbol": sym, "pnl": pnl, "size": 5.0,
         "ts_utc": "2026-11-07T10:00:00+00:00"}
    if net is not None:
        t["pnl_net"] = net
        t["cost"] = pnl - net
    return t


def _book(rows):
    return [_trade(s, p) for s, p, n in rows for _ in range(n)]


def test_leader_share_is_printed_when_book_is_positive():
    si = _load()
    # The 11/07 shape: one name carries the book, the rest bleed slower.
    # NOTE THE UNIT OF `pnl` — it is DOLLARS, and one trade pays a round trip of
    # ~$7.4 on a $5 stake at the measured 0.14852% RT. A "-1%" fixture is
    # 20x too small to survive the fee, so a loser here must lose dollars.
    book = (_book([("NEAR/USDT", 20.0, 60)])
            + _book([("ETH/USDT", -6.0, 30),
                     ("LINK/USDT", -6.0, 30),
                     ("SOL/USDT", -6.0, 30)]))
    lines = si._symbol_lines(book)
    best = [l for l in lines if l.startswith("paper best symbol")][0]
    assert "NEAR/USDT" in best
    assert "% of the book's net" in best, best
    share = int(best.split("% of the book's net")[0].rsplit(" ", 1)[1])
    # ~1200 winner - ~550 of losers => leader is >100% of the book's net
    assert share > 100, best


def test_no_share_percentage_on_a_book_whose_net_is_not_positive():
    si = _load()
    book = (_book([("ETH/USDT", -0.01, 30)])
            + _book([("LINK/USDT", -0.02, 30)]))
    lines = si._symbol_lines(book)
    best = [l for l in lines if l.startswith("paper best symbol")][0]
    assert "% of the book's net" not in best, best
    assert "leader IS the book" in best, best


def test_book_that_only_loses_still_reports_the_rest():
    """Real book, 11/07: leader +5.55, book net -5.02. The line must carry the
    negative rest-sum rather than a fabricated share."""
    si = _load()
    # Costs are ~$7.4 per trade on a $5 stake, so the loser legs must lose
    # enough dollars to drag the $1200 winner under zero: 60*$7.4 > $1200.
    book = (_book([("NEAR/USDT", 20.0, 60)])
            + _book([("ETH/USDT", -30.0, 30),
                     ("LINK/USDT", -30.0, 30)]))
    best = [l for l in si._symbol_lines(book)
            if l.startswith("paper best symbol")][0]
    assert "leader IS the book" in best, best
    assert "2 symbol(s) net" in best, best
    assert "-" in best.split("symbol(s) net")[1]


def test_share_is_re_derived_not_quoted():
    """Same shape, different magnitude -> the printed share changes (rule 9)."""
    si = _load()
    a = (_book([("NEAR/USDT", 0.02, 60)])
         + _book([("ETH/USDT", -0.001, 30), ("LINK/USDT", -0.001, 30)]))
    b = (_book([("NEAR/USDT", 0.02, 60)])
         + _book([("ETH/USDT", -0.02, 30), ("LINK/USDT", -0.02, 30)]))
    line_a = [l for l in si._symbol_lines(a) if l.startswith("paper best")][0]
    line_b = [l for l in si._symbol_lines(b) if l.startswith("paper best")][0]
    assert line_a != line_b


def test_losers_line_still_printed_and_counted():
    si = _load()
    book = (_book([("NEAR/USDT", 0.02, 40)])
            + _book([("ETH/USDT", -0.001, 25), ("LINK/USDT", -0.001, 25)]))
    lines = si._symbol_lines(book)
    losers = [l for l in lines if l.startswith("paper net-LOSING")]
    assert losers and "2 —" in losers[0], losers


def test_thin_sample_is_ignored():
    si = _load()
    book = (_book([("NEAR/USDT", 0.02, 25)]) + _book([("DUST/USDT", 9.0, 3)]))
    lines = si._symbol_lines(book)
    assert not any("DUST" in l for l in lines)
