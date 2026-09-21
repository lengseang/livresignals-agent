"""Auto-trader bridge tests (monkeypatched — no network, no orders)."""
from __future__ import annotations

import pytest

from trading.signals import auto_trader as at


def _sig(stype="dip_zone", symbol="BTC/USDT"):
    return {"signal_type": stype, "symbol": symbol, "ts_utc": "2026-08-12T00:00:00Z",
            "name": stype.upper(), "severity": "BIG", "emoji": "🟢", "reason": "test",
            "drawdown_pct": -0.05, "regime": "RANGING", "atr_ratio": 1.0}


def _stub_scorer_ok(monkeypatch):
    """Stub the quality gate so scorer always says TRADE (test isolation)."""
    import trading.signals.scorer as scorer
    monkeypatch.setattr(scorer, "score_signal", lambda sig: {
        "score": 85.0, "expected_value_pct": 0.16,
        "recommended_action": "TRADE", "learned": False,
    })


def test_info_signal_no_trade(monkeypatch):
    sig = _sig("regime_shift")  # not tradeable
    out = at.auto_trade_on_signal(sig)
    assert out["action"] == "info"


def test_universe_guard(monkeypatch):
    sig = _sig("dip_zone", "DOGE/USDT")
    out = at.auto_trade_on_signal(sig)
    assert out["action"] == "skip"
    assert "outside universe" in out["reason"]


def test_cadence_guard(monkeypatch):
    sig = _sig("dip_zone", "BTC/USDT")
    _stub_scorer_ok(monkeypatch)
    monkeypatch.setattr(at, "_guards_ok", lambda s: (True, "ok"))
    monkeypatch.setattr(at, "should_entry", lambda l, s, hours=24: (False, "next entry in 29d"))
    out = at.auto_trade_on_signal(sig)
    assert out["action"] == "skip"
    assert "cadence" in out["reason"]


def test_trade_executes(monkeypatch):
    sig = _sig("dip_zone", "BTC/USDT")
    sig["drawdown_pct"] = -0.19
    _stub_scorer_ok(monkeypatch)
    monkeypatch.setattr(at, "_guards_ok", lambda s: (True, "ok"))
    monkeypatch.setattr(at, "should_entry", lambda l, s, hours=24: (True, "due"))
    monkeypatch.setattr(at, "run_entry", lambda amt, sym, force=False: {
        "skipped": False, "symbol": sym, "drawdown_pct": -0.19, "multiplier": 1.5,
        "order": {"id": "T1", "cost": 99.5, "price": 63000.0, "amount": 0.00158, "status": "FILLED"}})
    monkeypatch.setattr(at, "_load_log", lambda: {"trades": [], "skips": []})
    monkeypatch.setattr(at, "_save_log", lambda d: None)
    out = at.auto_trade_on_signal(sig)
    assert out["action"] == "trade"
    assert out["multiplier"] == 1.5


def test_deep_dip_gets_2x(monkeypatch):
    sig = _sig("deep_dip", "ETH/USDT")
    sig["drawdown_pct"] = -0.27
    _stub_scorer_ok(monkeypatch)
    monkeypatch.setattr(at, "_guards_ok", lambda s: (True, "ok"))
    monkeypatch.setattr(at, "should_entry", lambda l, s, hours=24: (True, "due"))
    calls = {}
    def fake_run_entry(amt, sym, force=False):
        calls["amt"] = amt
        return {"skipped": False, "symbol": sym, "drawdown_pct": -0.27, "multiplier": 2.0,
                "order": {"id": "T2", "cost": amt, "price": 1800.0, "amount": 0.1, "status": "FILLED"}}
    monkeypatch.setattr(at, "run_entry", fake_run_entry)
    monkeypatch.setattr(at, "_load_log", lambda: {"trades": [], "skips": []})
    monkeypatch.setattr(at, "_save_log", lambda d: None)
    out = at.auto_trade_on_signal(sig, quote_amount=100.0)
    assert out["action"] == "trade"
    assert calls["amt"] == pytest.approx(200.0)  # 100 * 2.0 (deep dip)
    assert out["multiplier"] == 2.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))