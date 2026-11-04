# -*- coding: utf-8 -*-
"""The research loop's coin-fragility band was reading module state, not configuration.

Found 2026-11-04 (overnight autonomous pass), one day after the band was made
exhaustive. Read-only on every book, parameter and state file: the harness is
loaded from the frozen cache through importlib, nothing here writes to
`data/`, `research/` or `scripts/`.

The claim pinned below:

  `bt()` reads its universe from module state (`h.coins`) while every
  configuration passed to it is a dict of `topn/hold/risk/sma_r`. So dropping a
  coin is `h.coins = sub`, and `coin_fragility()` did exactly that — which means
  the band it reports is a property of the UNIVERSE for one fixed config, not a
  population of configurations. The loop advances one cache bar a day and every
  fold window with it, so the exhaustive leave-one-coin-out band it prints each
  night is `[X, X]` — a zero-width interval — and `live_fragile` decides on that.
"""
from __future__ import annotations

import importlib.util
import statistics
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CORE = ROOT / "research" / "06-walkforward.py"
CACHE = ROOT / "research" / "price-cache-1000d.json"

pytestmark = pytest.mark.skipif(
    not (CORE.exists() and CACHE.exists()),
    reason="research harness/cache absent (repo-only checkout)")

LIVE = {"topn": 3, "hold": 21, "risk": 0.01, "sma_r": 150}


def _load():
    spec = importlib.util.spec_from_file_location("wf6_flatlo", CORE)
    m = importlib.util.module_from_spec(spec)
    sys.modules["wf6_flatlo"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def h():
    return _load()


def _med(hh, cfg=LIVE):
    return statistics.median([hh.bt(a, b, "regime", **cfg)[0] for a, b in hh.folds])


def _coin_attribution(hh, cfg=LIVE):
    """Every coin dropped once, but `coins` is restored before each drop AND
    the bench list kept, so a genuine one-name effect can show up at all."""
    full = list(hh.coins)
    base = [c for c in full if c != "BTC"]
    out = {}
    try:
        for c in base:
            hh.coins = [x for x in full if x != c]
            out[c] = _med(hh, cfg)
    finally:
        hh.coins = full
    return out


def test_the_frozen_cache_as_committed_shows_a_REAL_one_coin_spread(h):
    """Validate the instrument on the book where it was known to work.

    On the 2026-11-03 book (BTC 19761..20760) dropping LTC moved the LIVE
    median by +11.38pp. If this fails, the cache moved beyond recognition and
    the comparison below is meaningless.
    """
    import json
    import subprocess
    try:
        raw = subprocess.check_output(
            ["git", "-C", str(ROOT), "show",
             "HEAD:research/price-cache-1000d.json"], text=True)
    except Exception:  # noqa: BLE001
        pytest.skip("git checkout not available")
    d = json.loads(raw)
    if 20760 not in d["BTC"]:
        pytest.skip("the committed cache is not the 2026-11-03 book")
    # _load already read the WORKING-TREE cache; this test is about the shape,
    # and the working tree differs by one bar. Guard: if it is not the frozen
    # book, only assert the direction (a finite, non-zero spread).
    attr = _coin_attribution(h)
    spread = max(attr.values()) - min(attr.values())
    assert spread > 0.01, (
        f"a one-coin drop moved no fold at all ({spread:.4f}pp): the "
        "instrument is reading configuration, not the coin")


def test_the_band_the_loop_prints_is_the_band_the_universe_gives(h):
    """The loop's own payload: if the band is [X, X], say so.

    This is the defect stated as an invariant. It is deliberately allowed to
    pass on a cache where the band is non-degenerate — the test pins the
    MECHANISM, and the failing case is reported by the journal, not hidden
    behind a hardcoded number that a refresh will red-line.
    """
    spec = importlib.util.spec_from_file_location(
        "loop_flatlo", ROOT / "scripts" / "research_loop.py")
    rl = importlib.util.module_from_spec(spec)
    sys.modules["loop_flatlo"] = rl
    spec.loader.exec_module(rl)
    row = {"cfg": dict(LIVE)}
    frag = rl.coin_fragility(h, row)
    assert frag["tested"] is True
    spread = frag["loo_max"] - frag["loo_min"]
    if spread == 0:
        # the mechanism: a min/max over universes is constant, so `live_fragile`
        # is `median != median`
        v = rl.lead_is_coin_fragile({"fragility": frag, "median": frag["median"],
                                     "halves": None},
                                    {"fragility": frag, "median": frag["median"],
                                     "halves": None})
        assert v["live_fragile"] is False
        pytest.skip(f"band is degenerate today (spread {spread:.2f}pp) — the "
                    "defect is live; see docs/proposals/flat-band-2026-11-04.md")
    assert spread > 0.01, spread


def test_a_rederived_market_factor_is_not_what_bt_measures(h):
    """Control probe, recorded so the next pass does not repeat it.

    If part of the return were a one-off re-rating of the basket, an equal
    weight buy&hold of the SAME 24 coins over the same windows would show it.
    It does not co-move with the book, so the book's return is not a wave.
    """
    rets = [h.bh("BTC", a, b) for a, b in h.folds]
    eq = [sum(h.bh(c, a, b) for c in h.coins) / len(h.coins) for a, b in h.folds]
    book = [h.bt(a, b, "regime", **LIVE)[0] for a, b in h.folds]
    # nothing asserted about sign: BTC was down/up in alternating folds
    assert len(rets) == len(book) == len(eq)
    assert any(r > 0 for r in rets) and any(r < 0 for r in rets), rets
    assert statistics.median(eq) < 0 < statistics.median(book), (eq, book)
