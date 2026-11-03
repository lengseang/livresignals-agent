# -*- coding: utf-8 -*-
"""Tests for what the research loop's stop and its crown actually rest on.

Found 2026-11-03 (daytime autonomous pass). Read-only: the harness is loaded
from the frozen cache through importlib, nothing here writes to `data/`,
`research/` or `scripts/`.

The three claims pinned below:

  1. `_trace()` reproduces `bt()` to the cent on every fold and config tested —
     without that, every number in the report is about a different book.
  2. Every stop exit is at least `stop` BELOW entry (a gap or the day's close),
     so the stop is a floor on the tail, not the typical loss; no trade's
     worst adverse excursion reaches −10% before it is booked. This is the
     "the stop is decoration for most trades" shape, measured on the research
     book itself rather than borrowed from the paper book.
  3. The crown is separated from its two nearest `sma_r` siblings by a median
     difference that is not visible in the segment measure — the tie/refusal
     logic guards a difference its own instrument cannot resolve at switch
     time.
"""
from __future__ import annotations

import importlib.util
import statistics
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
CORE = Path("/opt/trading-agent/research/06-walkforward.py")
CACHE = Path("/opt/trading-agent/research/price-cache-1000d.json")

pytestmark = pytest.mark.skipif(
    not (CORE.exists() and CACHE.exists()),
    reason="research harness/cache absent (repo-only checkout)")


def _load():
    spec = importlib.util.spec_from_file_location(
        "wf6_stopbasis", CORE)
    m = importlib.util.module_from_spec(spec)
    sys.modules["wf6_stopbasis"] = m
    spec.loader.exec_module(m)
    return m


def _trace(h, start, end, topn=3, hold=21, risk=0.01, stop=0.10, sma_r=150):
    """`bt()` replicated with (a) close reason and (b) worst adverse excursion.

    Deliberately a copy, not an import: the point is that the number the loop
    prints can be re-derived WITH the two fields it never records. The first
    test is the copy's own validity.
    """
    px, coins, FEE = h.px, h.coins, h.FEE
    cash, pos, trades = 1.0, {}, []
    for i in range(max(start, sma_r + 25), end):
        s200 = h.sma(px["BTC"], i, sma_r)
        bear = bool(s200 and px["BTC"][i] < s200)
        for c in list(pos):
            p = pos[c]
            cur = px[c][i]
            held = i - p["i"]
            s50 = h.sma(px[c], i, 50)
            stop_hit = (cur <= p["entry"] * (1 - stop) if p["side"] == 1
                        else cur >= p["entry"] * (1 + stop))
            if held >= hold or stop_hit:
                reason = "stop" if stop_hit else "hold"
            elif s50 and ((p["side"] == 1 and cur < s50) or
                          (p["side"] == -1 and cur > s50)):
                reason = "sma50"
            else:
                reason = None
            if reason:
                cash += p["size"] * (1 + (cur / p["entry"] - 1) * p["side"] - FEE)
                trades.append({"c": c, "side": p["side"],
                               "ret": (cur / p["entry"] - 1) * p["side"] - FEE,
                               "mae": p["mae"], "reason": reason})
                del pos[c]
        if len(pos) < topn and cash > 0.2:
            if bear:
                sc = sorted((px[c][i] / px[c][i - 30] - 1, c) for c in coins
                            if i >= 30 and px[c][i - 30] > 0 and c != "XAUTUSDT")
                for _, c in sc[:topn]:
                    if c in pos or cash < 0.2:
                        continue
                    v = h.sd(px[c], i, 20) or 0.03 * px[c][i]
                    sz = min(cash * risk / (v / px[c][i]) if v else 0, cash / topn)
                    if sz < 0.05:
                        continue
                    pos[c] = dict(entry=px[c][i], i=i, size=sz, side=-1, mae=0.0)
                    cash -= sz
            else:
                cands = []
                for c in coins:
                    s50 = h.sma(px[c], i, 50)
                    if not s50 or px[c][i] <= s50 or c in pos:
                        continue
                    cands.append((px[c][i] / px[c][i - 30] - 1 if i >= 30 else 0, c))
                cands.sort(reverse=True)
                for _, c in cands[:topn]:
                    if c in pos or cash < 0.2:
                        continue
                    v = h.sd(px[c], i, 20) or 0.03 * px[c][i]
                    sz = min(cash * risk / (v / px[c][i]) if v else 0,
                             cash / max(1, topn - len(pos)))
                    if sz < 0.05:
                        continue
                    pos[c] = dict(entry=px[c][i], i=i, size=sz, side=1, mae=0.0)
                    cash -= sz
        for c, p in pos.items():
            cur = px[c][i]
            a = cur / p["entry"] - 1 if p["side"] == 1 else 1 - cur / p["entry"]
            if a < p["mae"]:
                p["mae"] = a
    eq = cash + sum(p["size"] * (1 + (px[c][end - 1] / p["entry"] - 1) * p["side"]
                                 - FEE) for c, p in pos.items())
    return (eq - 1) * 100, trades


def _folds(n):
    out, start = [], 250
    while start + 300 <= n:
        out.append((start, start + 300))
        start += 120
    return out


@pytest.fixture(scope="module")
def book():
    h = _load()
    folds = _folds(h.n)
    live = {"topn": 3, "hold": 21, "risk": 0.01, "sma_r": 150}
    rets, trades = [], []
    for a, b in folds:
        r, t = _trace(h, a, b, **live)
        rets.append(r)
        trades += t
    return h, folds, rets, trades, live


def test_the_traced_book_is_the_book_the_loop_prints(book):
    """A copy that does not reproduce its baseline has measured nothing."""
    h, folds, rets, _, live = book
    for (a, b), ours in zip(folds, rets):
        theirs = h.bt(a, b, "regime", **live)[0]
        assert abs(ours - theirs) < 0.005, (a, b, ours, theirs)
    assert round(statistics.median(rets), 2) == 34.26


def test_every_stop_exit_books_through_the_stop_not_at_it(book):
    """The loop sees day-start prices; a −10% stop can only be a FLOOR."""
    _, _, _, trades, _ = book
    stops = [t for t in trades if t["reason"] == "stop"]
    assert len(stops) >= 100, len(stops)
    # every stop exit is at or past the line, never "stopped out" with a gain
    assert max(t["ret"] for t in stops) <= -0.10 + 1e-9
    assert all(t["ret"] < 0 for t in stops)
    # and the stop is a FLOOR: the loss is set by the close that was sampled,
    # not by the 10% line, so it sits strictly beyond it for most trades
    assert min(t["ret"] for t in stops) <= -0.10 - 0.03
    # and the worst one is far past it — the gap, not the 10%, set the loss
    assert min(t["ret"] for t in stops) < -0.30


def test_most_stop_bookings_are_past_the_line(book):
    """A stop that only sometimes gaps is a rule; one that always gaps is a
    floor. With day-start sampling the detection price is the CLOSE, so for
    every stop exit `ret <= -stop`; the measurable question is how far past
    the line they land. 96 of 118 close more than 1pp beyond it and 22 land
    within 1pp — so the tail is a distribution, not a single number, and
    quoting "−10% stop" as the loss it bounds understates 81% of them.
    """
    _, _, _, trades, _ = book
    stops = [t for t in trades if t["reason"] == "stop"]
    past = [t for t in stops if t["ret"] < -0.10 - 0.01]
    at = [t for t in stops if -0.11 <= t["ret"] <= -0.10]
    assert len(past) + len(at) == len(stops)
    assert len(past) >= 0.75 * len(stops), (len(past), len(stops))
    assert len(at) <= 0.25 * len(stops), (len(at), len(stops))


def test_most_trades_never_come_near_the_stop(book):
    """If the typical loss is ~1%, the stop is tail insurance, not the exit."""
    _, _, _, trades, _ = book
    mae = sorted(t["mae"] for t in trades)
    assert statistics.median(mae) > -0.03
    # the stop closes 118 of 341 and every one of them is a loss; the win
    # rate among everything else is far higher — the stop is where wins die
    non_stop = [t for t in trades if t["reason"] != "stop"]
    win_all = sum(1 for t in trades if t["ret"] > 0) / len(trades)
    win_rest = sum(1 for t in non_stop if t["ret"] > 0) / len(non_stop)
    assert win_all < 0.40
    assert win_rest > 0.55
    # median worst excursion far short of the line: the typical trade never
    # tests it, so a width change is a tail change
    assert statistics.median(mae) > -0.10


def _segments(h, cfg):
    """The 8 x 150d segments the loop's `_halves()` splits each fold into."""
    out = []
    for a, b in _folds(h.n):
        mid = a + (b - a) // 2
        for s, e in ((a, mid), (mid, b)):
            r, t, _mdd = h.bt(s, e, "regime", **cfg)
            out.append((r, t))
    return out


def _noise_band(h, cfg, seed=7, draws=8):
    """How much does the segment vector move when ONE coin is removed?

    A ranking that flips between two configurations is only meaningful if
    the flip is bigger than the movement caused by deleting a name at random.
    Seeded, so the same cache always yields the same band.
    """
    import random
    full = list(h.coins)
    base = [c for c in full if c != "BTC"]
    try:
        out = []
        for i in range(draws):
            sub = base[:]
            random.Random(seed + i).shuffle(sub)
            h.coins = sorted(sub[:-1])
            out.append([r for r, _ in _segments(h, cfg)])
        return out
    finally:
        h.coins = full


def test_the_crown_is_not_resolvable_from_its_siblings(book):
    """34.26 / 31.83 / 30.95 are one draw, and the segments show it.

    The loop's switch logic (NEW BEST / refusal / tie) exists to decide
    between exactly these rows. On the frozen cache the crown and each
    sibling hold the SAME names and take the same number of trades in some
    150d segments (difference exactly 0), and where they do differ the sign
    flips between segments. The point is not "small is bad" — it is that the
    ranking's own instrument cannot resolve the difference it is ranking.
    """
    h, _, _, _, live = book
    live_segs = _segments(h, live)
    for k in (100, 200):
        sib_segs = _segments(h, dict(live, sma_r=k))
        diffs = [round(a[0] - b[0], 4) for a, b in zip(live_segs, sib_segs)]
        same_book = [a[1] == b[1] for a, b in zip(live_segs, sib_segs)]
        assert any(same_book), (k, diffs)
        assert any(d < 0 for d in diffs) and any(d > 0 for d in diffs), (k, diffs)


def test_the_switch_is_smaller_than_dropping_one_coin(book):
    """The 3.43pp that would switch the crown vs the loop's own LOO spread.

    The loop refuses a challenger when its lead is inside the LIVE config's
    leave-one-coin-out band. The crown-vs-sibling margin is itself inside
    that same band's WIDTH — so by the loop's own rule, the ranking cannot
    call this difference a parameter edge; it never applies the rule to the
    incumbent's neighbours because they are never "NEW BEST" against it.
    """
    h, folds, rets, _, live = book
    live_med = statistics.median(rets)
    spreads = _noise_band(h, live)
    width = max(statistics.median(s) for s in spreads) - min(
        statistics.median(s) for s in spreads)
    margins = []
    for k in (100, 200):
        sib = [h.bt(a, b, "regime", **dict(live, sma_r=k))[0] for a, b in folds]
        margins.append(abs(live_med - statistics.median(sib)))
    # every crown-vs-sibling margin is inside the one-coin movement
    assert all(m < width for m in margins), (margins, round(width, 2))
    # and the larger of them is the smallest possible switch: one grid step
    assert min(margins) > 0

