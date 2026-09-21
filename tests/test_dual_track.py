"""Dual-track executor tests — routing, guards, caps, bug log.

These tests must NEVER touch real exchanges or real ledgers: every
exchange call and ledger write is monkeypatched. The live client is
exercised only at the "refuses without credentials" boundary.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading.allocation import confidence, dual_executor


# ---------------------------------------------------------------------------
# Confidence model
# ---------------------------------------------------------------------------
class TestConfidence:
    def test_deep_dip_scores_high(self):
        conf = confidence.compute_confidence(
            -0.25, detectors=["dip_zone", "vol_spike", "momentum_break"], regime="BEAR"
        )
        assert conf >= 0.60

    def test_no_dip_scores_low(self):
        conf = confidence.compute_confidence(-0.02, detectors=[], regime="BULL")
        assert conf < 0.40

    def test_entry_zone_weight_dominates(self):
        in_zone = confidence.compute_confidence(-0.16, detectors=[], regime="RANGING")
        out_zone = confidence.compute_confidence(-0.07, detectors=[], regime="RANGING")
        assert in_zone > out_zone

    def test_detectors_add_corroboration(self):
        alone = confidence.compute_confidence(-0.18, detectors=[], regime="RANGING")
        stacked = confidence.compute_confidence(
            -0.18, detectors=["dip_zone", "vol_spike"], regime="RANGING"
        )
        assert stacked > alone

    def test_confidence_clipped(self):
        conf = confidence.compute_confidence(
            -0.40, detectors=["dip_zone", "deep_dip", "vol_spike", "momentum_break"], regime="PANIC"
        )
        assert 0.0 <= conf <= 1.0


# ---------------------------------------------------------------------------
# Routing gates
# ---------------------------------------------------------------------------
class TestRouting:
    def test_live_disabled_by_default(self, tmp_path, monkeypatch):
        cfg = confidence._default_config()
        cfg["live_enabled"] = False
        assert not confidence.should_route_live(0.99, cfg)

    def test_live_requires_threshold(self, tmp_path, monkeypatch):
        cfg = confidence._default_config()
        cfg["live_enabled"] = True
        assert not confidence.should_route_live(0.40, cfg)
        assert confidence.should_route_live(0.80, cfg)

    def test_live_size_capped(self, tmp_path, monkeypatch):
        cfg = confidence._default_config()
        cfg["live_base_amount"] = 10.0
        cfg["live_max_per_entry"] = 50.0
        assert confidence.live_size(100.0, cfg) == 10.0
        cfg["live_base_amount"] = 99.0
        assert confidence.live_size(100.0, cfg) == 50.0  # hard cap


# ---------------------------------------------------------------------------
# Live ledger
# ---------------------------------------------------------------------------
class TestLiveLedger:
    def test_load_returns_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dual_executor, "LIVE_LEDGER_FILE", tmp_path / "nope.json")
        ledger = dual_executor.load_live_ledger()
        assert ledger["entries"] == []

    def test_save_load_roundtrip(self, tmp_path, monkeypatch):
        f = tmp_path / "live-ledger.json"
        monkeypatch.setattr(dual_executor, "LIVE_LEDGER_FILE", f)
        dual_executor.save_live_ledger({"entries": [{"symbol": "BTC/USDT", "track": "live"}]})
        loaded = dual_executor.load_live_ledger()
        assert loaded["entries"][0]["track"] == "live"

    def test_live_should_entry_cadence(self, tmp_path, monkeypatch):
        ledger = {"entries": [{"symbol": "BTC/USDT", "ts_ms": 0}]}
        due, reason = dual_executor.live_should_entry(ledger, "BTC/USDT", interval_days=30)
        assert due is True
        assert "cadence" in reason


# ---------------------------------------------------------------------------
# Bug log
# ---------------------------------------------------------------------------
class TestBugsLog:
    def test_log_bug_appends(self, tmp_path, monkeypatch):
        f = tmp_path / "bugs-log.md"
        monkeypatch.setattr(dual_executor, "BUGS_LOG_FILE", f)
        dual_executor.log_bug("test", "LOW", "forced test bug", "none")
        content = f.read_text(encoding="utf-8")
        assert "forced test bug" in content
        assert "| ts_utc |" in content  # header written on first append

    def test_log_bug_appends_twice(self, tmp_path, monkeypatch):
        f = tmp_path / "bugs-log.md"
        monkeypatch.setattr(dual_executor, "BUGS_LOG_FILE", f)
        dual_executor.log_bug("test", "LOW", "bug one", "")
        dual_executor.log_bug("test", "LOW", "bug two", "")
        content = f.read_text(encoding="utf-8")
        assert content.count("bug one") == 1
        assert content.count("bug two") == 1


# ---------------------------------------------------------------------------
# Router end-to-end (all mocks — no real orders)
# ---------------------------------------------------------------------------
class TestDualExecutorRunAll:
    def test_paper_only_when_live_disabled(self, tmp_path, monkeypatch):
        # Force config to live-disabled and stub every external call.
        cfg = confidence._default_config()
        monkeypatch.setattr(confidence, "load_config", lambda: cfg)
        monkeypatch.setattr(dual_executor, "load_config", lambda: cfg)
        monkeypatch.setattr(dual_executor, "live_enabled", lambda: False)

        monkeypatch.setattr(dual_executor, "_drawdown", lambda s: -0.19)
        monkeypatch.setattr(dual_executor, "_detectors", lambda s: ["dip_zone", "vol_spike"])
        monkeypatch.setattr(
            dual_executor, "_paper_due",
            lambda paper, sym, cfg_, force: (True, "test due"),
        )
        monkeypatch.setattr(
            dual_executor, "run_paper_entry",
            lambda quote, sym, force=False: {"skipped": False, "order": {"id": "PAPER-1"}},
        )
        # Live must never be called.
        def _boom(sym, size):
            raise AssertionError("live order must not fire when disabled")

        monkeypatch.setattr(dual_executor, "place_live_order", _boom)

        result = dual_executor.run_all(100.0, force=True)
        assert result["live_enabled"] is False
        for r in result["results"]:
            assert r["route"] == "paper"

    def test_live_fires_on_high_confidence(self, tmp_path, monkeypatch):
        cfg = confidence._default_config()
        cfg["live_enabled"] = True
        cfg["live_conf_threshold"] = 0.50
        monkeypatch.setattr(confidence, "load_config", lambda: cfg)
        monkeypatch.setattr(dual_executor, "load_config", lambda: cfg)
        monkeypatch.setattr(dual_executor, "live_enabled", lambda: True)

        monkeypatch.setattr(dual_executor, "_drawdown", lambda s: -0.22)
        monkeypatch.setattr(dual_executor, "_detectors", lambda s: ["dip_zone", "vol_spike"])
        monkeypatch.setattr(
            dual_executor, "_paper_due",
            lambda paper, sym, cfg_, force: (True, "test due"),
        )
        monkeypatch.setattr(
            dual_executor, "run_paper_entry",
            lambda quote, sym, force=False: {"skipped": False, "order": {"id": "PAPER-1"}},
        )
        # Fresh live ledger (no cadence history) so live_due is True.
        monkeypatch.setattr(dual_executor, "load_live_ledger", lambda: {"entries": []})
        placed = []

        def _fake_live(sym, size):
            placed.append((sym, size))
            return {"id": f"LIVE-{len(placed)}"}

        monkeypatch.setattr(dual_executor, "place_live_order", _fake_live)

        result = dual_executor.run_all(100.0, force=True)
        assert len(placed) == len(dual_executor.SYMBOLS)
        assert all(size <= 50.0 for _, size in placed)  # cap enforced

    def test_live_error_logs_bug_and_keeps_paper(self, tmp_path, monkeypatch):
        cfg = confidence._default_config()
        cfg["live_enabled"] = True
        cfg["live_conf_threshold"] = 0.50
        monkeypatch.setattr(confidence, "load_config", lambda: cfg)
        monkeypatch.setattr(dual_executor, "load_config", lambda: cfg)
        monkeypatch.setattr(dual_executor, "live_enabled", lambda: True)
        monkeypatch.setattr(dual_executor, "BUGS_LOG_FILE", tmp_path / "bugs-log.md")

        monkeypatch.setattr(dual_executor, "_drawdown", lambda s: -0.22)
        monkeypatch.setattr(dual_executor, "_detectors", lambda s: ["dip_zone", "vol_spike"])
        monkeypatch.setattr(
            dual_executor, "_paper_due",
            lambda paper, sym, cfg_, force: (True, "test due"),
        )
        monkeypatch.setattr(
            dual_executor, "run_paper_entry",
            lambda quote, sym, force=False: {"skipped": False, "order": {"id": "PAPER-1"}},
        )
        monkeypatch.setattr(dual_executor, "load_live_ledger", lambda: {"entries": []})

        def _fail_live(sym, size):
            raise RuntimeError("exchange down")

        monkeypatch.setattr(dual_executor, "place_live_order", _fail_live)

        result = dual_executor.run_all(100.0, force=True)
        bugs = (tmp_path / "bugs-log.md").read_text(encoding="utf-8")
        assert "live order failed" in bugs  # bug recorded
        # Paper still executed for every symbol despite live failure.
        for r in result["results"]:
            assert "live: exchange down" in r["error"]


# ---------------------------------------------------------------------------
# Live client refuses without credentials
# ---------------------------------------------------------------------------
class TestLiveClientBoundary:
    def test_make_live_exchange_raises_without_creds(self, tmp_path, monkeypatch):
        from trading.exchange import live_client

        missing = tmp_path / "live.env"
        missing.write_text("# no creds\n", encoding="utf-8")
        monkeypatch.setattr(live_client, "ENV_FILE", missing)
        with pytest.raises(ValueError):
            live_client.make_live_exchange()
