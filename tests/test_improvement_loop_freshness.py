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
    """A REAL named regime below 50% must still produce the stand-down warning."""
    regimes = [{"regime": "bear", "n": 30, "win_rate": 40.0, "avg_pnl": -0.01}]
    sugs = il._suggest({"n": 100, "win_rate": 50.0, "profit_factor": 1.2},
                       None, regimes, [], None)
    assert any(s["type"] == "regime_disable" for s in sugs), sugs
