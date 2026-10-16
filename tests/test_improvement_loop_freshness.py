"""The improvement loop must not present stale/absent attribution as current.

Regression tests for the defect found on 2026-10-10:

  * The per-edge stats section is the loop's "accuracy-improvement core", but
    the two ACTIVE paper openers (validated + loose) wrote NO `edge` tag, so
    the last edge-tagged trade was 2026-09-13 while the book ran to 2026-10-10.
    87% of trades fell back to loose/validated and the "DECAYING edge" verdicts
    were computed on a ~4-week-stale 362-trade subset, reported as if current.

  * No paper trade carries a `regime` field, so by_regime collapsed to one "?"
    bucket and the loop emitted a fake actionable "Regime ? ... stand down".

These tests pin the guards that make both loud instead of silent.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "improvement_loop", ROOT / "scripts" / "improvement_loop.py")
il = importlib.util.module_from_spec(_spec)
sys.modules["improvement_loop"] = il
_spec.loader.exec_module(il)


def _trade(ts_utc, edge=None, pnl=0.01, regime=None):
    t = {"ts_utc": ts_utc, "pnl": pnl, "symbol": "BTC/USDT"}
    if edge:
        t["edge"] = edge
    if regime:
        t["regime"] = regime
    return t


def test_edge_freshness_flags_stale_feed():
    old = (datetime.now(timezone.utc) - timedelta(days=26)).isoformat()
    trades = [_trade(old, edge="supertrend") for _ in range(5)]
    trades += [_trade((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
               for _ in range(20)]                      # untagged recent trades
    meta = il._edge_freshness(trades)
    assert meta["stale"] is True, meta
    assert meta["stale_days"] >= 25
    assert meta["tagged_pct"] < 50
    assert "NOT current" in meta["note"]


def test_edge_freshness_ok_when_recent_and_dense():
    now = datetime.now(timezone.utc)
    trades = [_trade((now - timedelta(hours=i)).isoformat(), edge="validated")
              for i in range(10)]
    meta = il._edge_freshness(trades)
    assert meta["stale"] is False, meta
    assert meta["tagged_pct"] == 100.0


def test_healthily_tagged_book_is_not_flagged_stale():
    """THE REAL-WORLD CASE (2026-10-13). A book with a large LEGACY untagged
    population and a healthy, recent, fully-tagged tail must NOT be flagged.

    Mirrors the real book: the legacy era (untagged, older) is followed by a
    continuous tagged era, with a single transition trade on the changeover day.
    Before the fix, `tagged_pct` was `tagged / len(all trades)`, and the legacy
    tail could never be tagged — so the guard fired on every run forever even
    while the live feed was provably healthy (stale_days 0.8). This test fails
    on that code.
    """
    now = datetime.now(timezone.utc)
    # Legacy era: 2751 trades, all written before any opener stamped `edge`.
    legacy = [_trade((now - timedelta(days=200 - i % 45)).isoformat())
              for i in range(2751)]
    # The one transition trade that slipped through untagged on the changeover.
    transition = _trade((now - timedelta(days=3)).isoformat())
    # Tagged era: 137 recent trades, every one tagged (the live feed, healthy).
    tagged = [_trade((now - timedelta(hours=i / 4)).isoformat(), edge="validated")
              for i in range(137)]
    meta = il._edge_freshness(legacy + [transition] + tagged)
    assert meta["stale"] is False, meta
    assert meta["stale_days"] < 2
    assert meta["tagged_pct"] < 50          # whole-book figure is still low …
    assert meta["recent_tagged_pct"] >= 95  # … but the recent feed is healthy
    assert meta["recent_n"] >= 100          # window is meaningful, not 1 trade
    assert meta["note"] == "", meta


def test_recent_untagged_tail_still_flags():
    """The guard keeps its teeth: a healthy legacy base does NOT excuse a
    recent tail that stopped being tagged."""
    now = datetime.now(timezone.utc)
    legacy = [_trade((now - timedelta(days=200 - i % 45)).isoformat())
              for i in range(500)]
    # Continuous tagged era, then it stops: 60 recent untagged trades at the top.
    good = [_trade((now - timedelta(days=20 - i / 6)).isoformat(), edge="loose")
            for i in range(60)]
    bad_tail = [_trade((now - timedelta(hours=i)).isoformat())
                for i in range(60)]
    meta = il._edge_freshness(legacy + good + bad_tail)
    assert meta["recent_n"] >= 60, meta
    assert meta["recent_tagged_pct"] < 50, meta
    assert meta["note"], meta
    assert "NOT current" in meta["note"]


def test_stale_feed_raises_a_priority_1_suggestion():
    now = datetime.now(timezone.utc)
    stale = il._edge_freshness(
        [_trade((now - timedelta(days=26)).isoformat(), edge="x")])
    paper = {"n": 100, "win_rate": 50.0, "profit_factor": 1.2}
    sugs = il._suggest(paper, None, [], [], stale)
    assert any(s["type"] == "stale_edge_data" and s["priority"] == 1
               for s in sugs), sugs


def test_unknown_regime_is_a_data_gap_not_a_stand_down():
    """One '?' bucket holding the whole book must NOT read as a regime signal."""
    regimes = [{"regime": "?", "n": 2778, "win_rate": 47.8, "avg_pnl": 0.0}]
    sugs = il._suggest({"n": 100, "win_rate": 50.0, "profit_factor": 1.2},
                       None, regimes, [], None)
    types = {s["type"] for s in sugs}
    assert "regime_data_gap" in types, sugs
    assert "regime_disable" not in types, sugs


def test_named_low_regime_still_warns():
    """A REAL named regime below 50% must still produce the stand-down warning —
    but ONLY when a second named regime exists to contrast it against."""
    regimes = [
        {"regime": "bear", "n": 30, "win_rate": 40.0, "avg_pnl": -0.01},
        {"regime": "bull", "n": 45, "win_rate": 58.0, "avg_pnl": 0.01},
    ]
    sugs = il._suggest({"n": 100, "win_rate": 50.0, "profit_factor": 1.2},
                       None, regimes, [], None)
    assert any(s["type"] == "regime_disable" for s in sugs), sugs


def test_single_named_regime_is_a_data_gap_not_a_stand_down():
    """THE REAL-WORLD CASE (2026-10-16). The `regime` field first reaches the
    paper book on 2026-10-10 and EVERY tagged trade since carries `RANGING`
    (142 of them, no other value). One named regime beside a large "?" bucket
    means the RANGING cohort IS the recent period — "Regime RANGING at 40.8% —
    stand down" is just the book's last-week win rate relabelled as a regime
    effect, and cannot be attributed to the regime. The loop must refuse the
    per-regime verdict (same trap the "?" guard prevents) rather than drive a
    stand-down decision on zero contrast. Fails on the pre-2026-10-16 code,
    which warned on any single named low regime.
    """
    regimes = [
        {"regime": "?", "n": 2326, "win_rate": 47.5, "avg_pnl": -0.0009},
        {"regime": "RANGING", "n": 142, "win_rate": 40.8, "avg_pnl": -0.0108},
    ]
    sugs = il._suggest({"n": 100, "win_rate": 50.0, "profit_factor": 1.2},
                       None, regimes, [], None)
    types = {s["type"] for s in sugs}
    assert "regime_disable" not in types, sugs
    gap = [s for s in sugs if s["type"] == "regime_data_gap"]
    assert gap, sugs
    assert "ONE value" in gap[0]["idea"], gap[0]["idea"]


def test_one_named_regime_below_the_n_floor_does_not_warn():
    """A single named regime too thin to judge (n<10) must not warn either."""
    regimes = [
        {"regime": "?", "n": 2326, "win_rate": 47.5, "avg_pnl": -0.0009},
        {"regime": "RANGING", "n": 7, "win_rate": 20.0, "avg_pnl": -0.02},
    ]
    sugs = il._suggest({"n": 100, "win_rate": 50.0, "profit_factor": 1.2},
                       None, regimes, [], None)
    assert "regime_disable" not in {s["type"] for s in sugs}, sugs
