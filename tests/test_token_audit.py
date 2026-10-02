"""token_audit tests — the crash guard and the fail-closed semantics.

Regression for the 2026-10-01 bug: native SOL returns `extraInfo: None`, and the
old code did `data.get("extraInfo", {}).get("buyTax")`. The `{}` default does NOT
apply when the key exists with a None value, so audit() raised
AttributeError: 'NoneType' object has no attribute 'get' on every native-SOL
probe. No network: _request is monkeypatched.
"""
from __future__ import annotations

from trading.signals import token_audit as ta


def _resp(data):
    return {"success": True, "code": "000000", "data": data}


def test_extra_info_none_does_not_crash(monkeypatch):
    """The exact live shape for native SOL: extraInfo is present-and-null."""
    monkeypatch.setattr(ta, "_request", lambda *a, **k: _resp({
        "hasResult": False, "isSupported": False,
        "riskLevelEnum": "LOW", "riskLevel": 0,
        "extraInfo": None, "riskItems": [],
    }))
    r = ta.audit("CT_501", "So11111111111111111111111111111111111111111")
    assert r["success"] is True           # must not raise
    assert r["buy_tax"] is None
    assert r["sell_tax"] is None
    assert r["has_result"] is False


def test_extra_info_missing_also_ok(monkeypatch):
    monkeypatch.setattr(ta, "_request", lambda *a, **k: _resp({
        "hasResult": True, "isSupported": True, "riskLevelEnum": "LOW",
        "riskLevel": 1, "riskItems": [],
    }))
    r = ta.audit("1", "0xabc")
    assert r["buy_tax"] is None


def test_hits_extracted(monkeypatch):
    monkeypatch.setattr(ta, "_request", lambda *a, **k: _resp({
        "hasResult": True, "isSupported": True, "riskLevelEnum": "HIGH",
        "riskLevel": 4, "extraInfo": {"buyTax": "0", "sellTax": "5"},
        "riskItems": [{"id": "CONTRACT_RISK", "details": [
            {"title": "Rug Pull Risk", "isHit": True},
            {"title": "Whitelist Restrictions Not Found", "isHit": False},
        ]}],
    }))
    r = ta.audit("56", "0xdef")
    assert r["risk_level"] == "HIGH"
    assert r["risk_code"] == 4
    assert r["hits"] == {"CONTRACT_RISK": ["Rug Pull Risk"]}
    assert r["issues"] == ["Rug Pull Risk"]


def test_fail_open_on_network_failure(monkeypatch):
    """is_safe() must return None (not False) when the probe never completed —
    a network blip must not block a trade."""
    monkeypatch.setattr(ta, "_request",
                        lambda *a, **k: {"success": False, "error": "timeout"})
    r = ta.audit("1", "0xabc")
    assert r["success"] is False
    assert ta.is_safe(r) is None


def test_unsupported_token_is_not_safe(monkeypatch):
    """Native tokens return isSupported=False -> is_safe None (cannot judge),
    NEVER True."""
    monkeypatch.setattr(ta, "_request", lambda *a, **k: _resp({
        "hasResult": False, "isSupported": False, "riskLevelEnum": "LOW",
        "riskLevel": 0, "extraInfo": None, "riskItems": [],
    }))
    assert ta.is_safe(ta.audit("CT_501", "So1...")) is None


def test_high_risk_is_vetoed_when_supported(monkeypatch):
    monkeypatch.setattr(ta, "_request", lambda *a, **k: _resp({
        "hasResult": True, "isSupported": True, "riskLevelEnum": "HIGH",
        "riskLevel": 4, "extraInfo": {}, "riskItems": [],
    }))
    assert ta.is_safe(ta.audit("1", "0xabc"), max_risk="LOW") is False
