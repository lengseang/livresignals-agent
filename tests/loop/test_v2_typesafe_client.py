"""V2-8: the real Jev client, tested against a fake transport. NO network, NO key.

PROVENANCE: the wire shapes below are from third-party write-ups of the TypeSafe API (the vendor docs are
unreachable from the build sandbox). They are UNVERIFIED against the live API until jev_probe is run with a key.
"""
import json
import logging
import urllib.error
import urllib.request

import pytest

from trading.v2.adapter import JevAdapter, JevProtocolError
from trading.v2.fallback import StaticSource
from trading.v2.contracts import FALLBACK_STRATEGIES
from trading.v2.jev_probe import probe
from trading.v2.typesafe_client import (JevAuthError, JevBadRequest, JevMissingKey, JevOverloaded, JevRateLimited,
                                        JevTransportError, TypeSafeJevClient, UrllibTransport, build_wire_request,
                                        translate_response, KEY_ENV, WIRE_URL)
from trading.v2.workflow import WORKFLOW_DIR, load_active, load_workflow

from test_v2_contracts import decision_state, feature_snapshot
from trading.v2.contracts import DecisionState, FeatureSnapshot

KEY = "sk-test-SECRET-0123456789"
WF2 = load_workflow(WORKFLOW_DIR / "crypto_trading_v2.json")
ALL_LONG = StaticSource({n: "LONG" for n in FALLBACK_STRATEGIES})
STATE = DecisionState.from_dict(decision_state())
FEAT = FeatureSnapshot.from_dict(feature_snapshot())      # snapshot_id matches STATE.features

# --- third-party documented examples (UNVERIFIED) ---------------------------------------------------------
DOC_CHOICE_REPLY = {"model": "jev-1.13.0", "answers": {"department": {
    "type": "choice", "choice": "billing", "probabilities": {"billing": 0.94, "technical": 0.05, "sales": 0.01},
    "confidence": 0.92}}, "usage": {"input_tokens": 180, "output_tokens": 24}}
DOC_NOUL_REPLY = {"model": "jev-1.13.0", "answers": {"refund_requested": {"type": "noul", "noul": 0.91}},
                  "usage": {"input_tokens": 420, "output_tokens": 52}}


def full_wire(action="LONG", dist=None, conf=0.9, **over):
    dist = dist or {"LONG": 0.92, "SHORT": 0.02, "HOLD": 0.03, "EXIT": 0.01, "REDUCE": 0.01, "NO_TRADE": 0.01}
    answers = {
        "market_regime": {"type": "choice", "choice": "TREND_UP",
                          "probabilities": {"TREND_UP": 0.8, "TREND_DOWN": 0.05, "RANGE": 0.05, "BREAKOUT": 0.05,
                                            "HIGH_VOLATILITY": 0.03, "LOW_LIQUIDITY": 0.02}, "confidence": 0.78},
        "trend_quality": {"type": "noul", "noul": 0.9}, "breakout_probability": {"type": "noul", "noul": 0.6},
        "reversal_probability": {"type": "noul", "noul": 0.2}, "entry_quality": {"type": "noul", "noul": 0.81},
        "expected_edge": {"type": "noul", "noul": 0.7}, "timing_quality": {"type": "noul", "noul": 0.75},
        "risk_level": {"type": "choice", "choice": "MEDIUM",
                       "probabilities": {"LOW": 0.1, "MEDIUM": 0.7, "HIGH": 0.15, "EXTREME": 0.05}, "confidence": 0.7},
        "action": {"type": "choice", "choice": action, "probabilities": dist, "confidence": conf},
    }
    wire = {"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": 700, "output_tokens": 60}}
    wire.update(over)
    return wire


class Fake:
    def __init__(self, status=200, body=None, raw=None, exc=None):
        self.status, self.body, self.raw, self.exc, self.calls = status, body, raw, exc, []

    def post(self, url, headers, body, timeout_s):
        self.calls.append((url, dict(headers), json.loads(body), timeout_s))
        if self.exc:
            raise self.exc
        return self.status, (self.raw if self.raw is not None else json.dumps(self.body).encode())


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)


def adapter(transport, **kw):
    return JevAdapter(TypeSafeJevClient(WF2, transport, **kw), WF2, ALL_LONG, timeout_s=2.0, clock=lambda: 0.0)


# ------------------------------------------------------------ request building

def test_wire_request_has_the_documented_shape():
    wire = build_wire_request(WF2, {"state": STATE.to_dict(), "features": FEAT.to_dict()})
    assert set(wire) == {"state", "model", "questions"} and wire["model"] == "jev-latest"
    assert wire["state"] == {**STATE.to_dict(), "features": FEAT.to_dict()}      # the VALUES, not the opaque id
    assert wire["state"]["features"]["technical"]["rsi"] == 67.8 and wire["state"]["features"]["returns"]["4h"] == 0.027
    q = wire["questions"]
    assert set(q) == set(WF2.questions)
    assert {n for n, v in q.items() if v["type"] == "noul"} == {
        "trend_quality", "breakout_probability", "reversal_probability", "entry_quality", "expected_edge",
        "timing_quality"}
    assert q["trend_quality"] == {"type": "noul", "instructions": WF2.questions["trend_quality"].instructions}
    assert q["action"]["type"] == "choice" and set(q["action"]["criteria"]) == {
        "LONG", "SHORT", "HOLD", "EXIT", "REDUCE", "NO_TRADE"}
    assert all(v["instructions"].strip() for v in q.values())
    text = build_wire_request(WF2, {"state": STATE.to_dict(), "features": FEAT.to_dict()}, state_as_text=True)
    assert isinstance(text["state"], str) and json.loads(text["state"])["features"] == FEAT.to_dict()


def test_a_workflow_without_question_text_cannot_be_sent():
    with pytest.raises(ValueError, match="no instructions"):
        build_wire_request(load_active(), {"state": {}, "features": {}})
    with pytest.raises(ValueError, match="no instructions"):
        TypeSafeJevClient(load_active(), Fake())


def test_the_url_must_be_https():
    with pytest.raises(ValueError, match="https"):
        TypeSafeJevClient(WF2, Fake(), url="http://api.typesafe.ai/v1/systemone")
    assert TypeSafeJevClient(WF2, Fake()).url == WIRE_URL == "https://api.typesafe.ai/v1/systemone"


def test_the_v2_workflow_is_a_new_version_and_v1_is_untouched():
    assert WF2.tag == "crypto_trading-v2" and load_active().tag == "crypto_trading-v1"
    assert {n: q.type for n, q in WF2.questions.items()} == {n: q.type for n, q in load_active().questions.items()}


# ------------------------------------------------------------ translation (strict)

def test_documented_examples_translate():
    wf = WF2
    # the doc examples use other question names, so check them through a tiny structural translation
    t = translate_response({"model": "jev-1.13.0", "answers": {"x": DOC_CHOICE_REPLY["answers"]["department"]}}, wf)
    assert t["answers"]["x"] == {"value": "billing", "probability": 0.92,
                                 "distribution": {"billing": 0.94, "technical": 0.05, "sales": 0.01}}
    t = translate_response({"model": "m", "answers": {"y": DOC_NOUL_REPLY["answers"]["refund_requested"]}}, wf)
    assert t["answers"]["y"] == {"value": 0.91}


def test_choice_without_confidence_uses_the_probability_of_the_chosen_option():
    a = {"type": "choice", "choice": "billing", "probabilities": {"billing": 0.94, "sales": 0.06}}
    assert translate_response({"model": "m", "answers": {"x": a}}, WF2)["answers"]["x"]["probability"] == 0.94


@pytest.mark.parametrize("bad", [
    None, [], "x", {}, {"model": "m"}, {"model": "", "answers": {}}, {"model": 5, "answers": {}},
    {"model": "m", "answers": []}, {"model": "m", "answers": {"x": "str"}},
    {"model": "m", "answers": {"x": {"type": "mystery"}}},
    {"model": "m", "answers": {"x": {"type": "noul", "noul": 1.2}}},
    {"model": "m", "answers": {"x": {"type": "noul", "noul": -0.1}}},
    {"model": "m", "answers": {"x": {"type": "noul", "noul": "high"}}},
    {"model": "m", "answers": {"x": {"type": "noul", "noul": True}}},
    {"model": "m", "answers": {"x": {"type": "noul", "noul": float("nan")}}},
    {"model": "m", "answers": {"x": {"type": "noul"}}},
    {"model": "m", "answers": {"x": {"type": "choice", "choice": "a", "probabilities": {"b": 1.0}}}},
    {"model": "m", "answers": {"x": {"type": "choice", "choice": "a", "probabilities": {"a": 1.5}}}},
    {"model": "m", "answers": {"x": {"type": "choice", "choice": "a", "probabilities": {"a": 0.5}, "confidence": 2}}},
    {"model": "m", "answers": {"x": {"type": "choice", "choice": 5, "probabilities": {"a": 0.5}}}},
    {"model": "m", "answers": {"x": {"type": "choice", "choice": "a"}}},
])
def test_malformed_replies_are_rejected_never_coerced(bad):
    with pytest.raises(Exception) as e:
        translate_response(bad, WF2)
    assert isinstance(e.value, (JevProtocolError,)) or type(e.value).__name__ == "JevMalformedJson"


def test_a_type_mismatch_with_what_we_asked_is_rejected():
    with pytest.raises(JevProtocolError, match="asked yes/no"):
        translate_response({"model": "m", "answers": {"trend_quality": {
            "type": "choice", "choice": "a", "probabilities": {"a": 1.0}}}}, WF2)
    with pytest.raises(JevProtocolError, match="asked choice"):
        translate_response({"model": "m", "answers": {"action": {"type": "noul", "noul": 0.5}}}, WF2)


# ------------------------------------------------------------ end to end through the adapter

def test_a_documented_reply_becomes_a_typed_jev_decision_through_the_whole_adapter():
    tr = Fake(body=full_wire())
    a = adapter(tr)
    d = a.evaluate(STATE, FEAT)
    assert d.source == "jev" and d.model == "jev-1.13.0" and d.workflow_version == "crypto_trading-v2"
    assert d.action == "LONG" and d.confidence == 0.9           # min(distribution 0.92, Jev's confidence 0.9)
    assert d.signals.entry_quality == 0.81 and d.risk_level == "MEDIUM"
    assert d.probabilities.LONG == 0.92 and d.questions.market_regime.answer == "TREND_UP"
    url, headers, body, timeout = tr.calls[0]
    assert url == WIRE_URL and headers["Authorization"] == f"Bearer {KEY}" and timeout == 2.0
    assert body["model"] == "jev-latest" and a.stats["jev"] == 1
    assert body["state"]["features"] == FEAT.to_dict()                  # Jev saw the numbers, not "feat_..."


def test_jevs_own_lower_confidence_lowers_the_decision_confidence_and_a_higher_one_never_raises_it():
    assert adapter(Fake(body=full_wire(conf=0.6))).evaluate(STATE, FEAT).confidence == 0.6
    assert adapter(Fake(body=full_wire(conf=0.99))).evaluate(STATE, FEAT).confidence == 0.92


def test_an_unknown_action_option_is_unknown_enum_and_falls_back():
    wire = full_wire(action="BUY", dist={"BUY": 0.9, "SELL": 0.1})
    d = adapter(Fake(body=wire)).evaluate(STATE, FEAT)
    assert d.source == "fallback" and d.fallback_reason.startswith("unknown_enum")


# ------------------------------------------------------------ errors -> categories, never text

@pytest.mark.parametrize("status,cls,category", [
    (401, "JevAuthError", "http_error"), (403, "JevAuthError", "http_error"),
    (402, "JevBalanceExhausted", "balance_exhausted"), (422, "JevBadRequest", "http_error"),
    (400, "JevBadRequest", "http_error"), (429, "JevRateLimited", "http_error"),
    (529, "JevOverloaded", "http_error"), (500, "JevOverloaded", "http_error"), (503, "JevOverloaded", "http_error"),
    (418, "JevHttpError", "http_error"), (302, "JevHttpError", "http_error"),
])
def test_http_statuses_map_to_exception_classes_and_labelled_fallbacks(status, cls, category):
    body = {"error": {"message": f"bad key {KEY} for account acct_123", "code": "payment_required"}}
    a = adapter(Fake(status=status, body=body))
    d = a.evaluate(STATE, FEAT)
    assert d.source == "fallback" and d.fallback_reason.startswith(f"{category}: {cls}")
    assert KEY not in d.fallback_reason and "acct_123" not in d.fallback_reason and "bad key" not in d.fallback_reason
    assert a.stats[f"fallback:{category}"] == 1 and a.client.usage[f"http_{status}"] == 1


def test_missing_api_key_falls_back_without_a_call(monkeypatch):
    monkeypatch.delenv(KEY_ENV)
    tr = Fake(body=full_wire())
    d = adapter(tr).evaluate(STATE, FEAT)
    assert d.fallback_reason.startswith("http_error: JevMissingKey") and tr.calls == []
    monkeypatch.setenv(KEY_ENV, "   ")
    assert adapter(tr).evaluate(STATE, FEAT).fallback_reason.startswith("http_error: JevMissingKey") and tr.calls == []


@pytest.mark.parametrize("raw,reason", [(b"<html>gateway error</html>", "malformed_json"), (b"", "malformed_json"),
                                        (b"[1,2]", "malformed_json"), (b"null", "malformed_json"),
                                        (b'{"model":"m"}', "schema_violation")])
def test_bad_bodies_on_200_fall_back_with_the_right_category(raw, reason):
    d = adapter(Fake(raw=raw)).evaluate(STATE, FEAT)
    assert d.source == "fallback" and d.fallback_reason.startswith(reason)


def test_transport_failures_are_classified_not_crashed():
    d = adapter(Fake(exc=JevTransportError("TimeoutError"))).evaluate(STATE, FEAT)
    assert d.fallback_reason.startswith("http_error: JevTransportError")


def test_the_key_never_appears_in_repr_logs_or_reasons(caplog):
    tr = Fake(status=401, body={"error": {"message": KEY}})
    a = adapter(tr)
    with caplog.at_level(logging.DEBUG):
        d = a.evaluate(STATE, FEAT)
        adapter(Fake(body=full_wire())).evaluate(STATE, FEAT)
    everything = repr(a.client) + d.to_json() + "\n".join(r.getMessage() for r in caplog.records) + repr(a.stats)
    assert KEY not in everything and "SECRET" not in everything
    assert KEY not in json.dumps(a.client.usage)


# ------------------------------------------------------------ usage and cost

def test_token_usage_is_tracked_and_cost_needs_the_owners_price():
    tr = Fake(body=full_wire())
    a = adapter(tr)
    for _ in range(3):
        a.evaluate(STATE, FEAT)
    u = a.client.usage
    assert (u["calls"], u["ok"], u["input_tokens"], u["output_tokens"], u["http_200"]) == (3, 3, 2100, 180, 3)
    assert a.client.estimated_cost_usd(0.42) == pytest.approx(2100 / 1e6 * 0.42)
    with pytest.raises(TypeError):
        a.client.estimated_cost_usd()                              # no default price: sources disagree


def test_usage_ignores_garbage_token_counts():
    wire = full_wire(usage={"input_tokens": True, "output_tokens": -5})
    a = adapter(Fake(body=wire)); a.evaluate(STATE, FEAT)
    assert a.client.usage["input_tokens"] == 0 and a.client.usage["output_tokens"] == 0


# ------------------------------------------------------------ the urllib transport (patched, no socket)

def test_urllib_transport_returns_http_error_statuses_and_hides_transport_error_text(monkeypatch):
    class R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok":1}'
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: R())
    assert UrllibTransport().post("https://x", {"A": "b"}, b"{}", 1.0) == (200, b'{"ok":1}')

    def http_err(req, timeout):
        raise urllib.error.HTTPError("https://x", 402, "pay", {}, __import__("io").BytesIO(b'{"error":{"code":"payment_required"}}'))
    monkeypatch.setattr(urllib.request, "urlopen", http_err)
    assert UrllibTransport().post("https://x", {}, b"{}", 1.0)[0] == 402

    def boom(req, timeout):
        raise urllib.error.URLError(f"cannot reach host with key {KEY}")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(JevTransportError) as e:
        UrllibTransport().post("https://x", {}, b"{}", 1.0)
    assert KEY not in str(e.value) and str(e.value) == "URLError"


# ------------------------------------------------------------ the probe

def test_probe_reports_a_matching_reply_and_saves_the_raw_body_without_the_key(tmp_path):
    out = tmp_path / "raw.json"
    rep = probe(Fake(body=full_wire()), WF2, True, out)
    assert rep["ok"] and rep["status"] == 200 and rep["model"] == "jev-1.13.0"
    assert sorted(rep["answers"]) == sorted(WF2.questions) and rep["usage"] == {"input_tokens": 700, "output_tokens": 60}
    assert json.loads(out.read_text())["model"] == "jev-1.13.0"
    assert KEY not in json.dumps(rep) and KEY not in out.read_text()


def test_probe_reports_each_way_the_real_api_could_disagree(tmp_path):
    assert probe(Fake(), WF2, False)["error"] == f"{KEY_ENV} is not set"
    r = probe(Fake(status=401, body={"error": {"code": "invalid_api_key"}}), WF2, True, tmp_path / "e.json")
    assert not r["ok"] and r["error"].startswith("HTTP 401") and (tmp_path / "e.json").exists()
    wrong = full_wire(); wrong["answers"]["entry_quality"] = {"type": "noul", "score": 0.8}      # different field name
    r = probe(Fake(body=wrong), WF2, True)
    assert not r["ok"] and r["error"].startswith("schema_violation") and "entry_quality" in r["error"]


# ------------------------------------------------------------ Jev must see the feature VALUES (found while costing)

def test_the_client_refuses_to_ask_jev_without_the_feature_values():
    tr = Fake(body=full_wire())
    d = adapter(tr).evaluate(STATE)                                   # no features supplied
    assert d.source == "fallback" and d.fallback_reason.startswith("client_error: JevMissingFeatures")
    assert tr.calls == []                                             # nothing was sent: no blind decision, no cost
    for bad in (None, "feat_01J8ZABCDEFGH", [], {}):
        with pytest.raises(Exception):
            build_wire_request(WF2, {"state": STATE.to_dict(), "features": bad})


def test_features_that_belong_to_another_state_are_refused():
    other = dict(feature_snapshot(), snapshot_id="feat_OTHERSNAPSHOT")
    d = adapter(Fake(body=full_wire())).evaluate(STATE, FeatureSnapshot.from_dict(other))
    assert d.source == "fallback" and d.fallback_reason.startswith("client_error: ValueError")
    with pytest.raises(Exception):
        build_wire_request(WF2, {"state": STATE.to_dict(), "features": other})


def test_the_internal_request_carries_the_values_and_stays_credential_free():
    from trading.v2.adapter import build_request
    req = build_request(STATE, WF2, STATE.state_hash(), FEAT)
    assert req["features"] == FEAT.to_dict() and req["state"]["features"] == FEAT.snapshot_id
    blob = json.dumps(req).lower()
    assert not any(w in blob for w in ("api_key", "secret", "password", "token", "bearer", KEY.lower()))
    with pytest.raises(ValueError, match="do not belong"):
        build_request(STATE, WF2, STATE.state_hash(), FeatureSnapshot.from_dict(dict(feature_snapshot(), snapshot_id="feat_X")))


def test_probe_reports_a_network_failure_cleanly_instead_of_crashing_and_never_leaks_the_key():
    r = probe(Fake(exc=JevTransportError("URLError")), WF2, True)
    assert not r["ok"] and "did not reach the API" in r["error"] and "URLError" in r["error"]
    assert KEY not in json.dumps(r) and "status" not in r


def test_the_answering_model_build_is_counted_so_a_silent_upgrade_is_visible():
    a = adapter(Fake(body=full_wire()))
    a.evaluate(STATE, FEAT); a.evaluate(STATE, FEAT)
    assert a.stats["model:jev-1.13.0"] == 2
    a.client.transport = Fake(body=full_wire(model="jev-1.14.0"))
    a.evaluate(STATE, FEAT)
    assert a.stats["model:jev-1.14.0"] == 1 and a.stats["jev"] == 3


def test_the_apis_own_score_answers_are_rejected_because_they_are_not_0_to_1():
    score = {"type": "score", "score": 1.43, "legend": {"0": "low", "1": "mid", "2": "high"},
             "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3}, "confidence": 0.6}
    with pytest.raises(JevProtocolError, match="unknown answer type"):
        translate_response({"model": "jev-1.13.0", "answers": {"trend_quality": score}}, WF2)


def test_a_pinned_model_is_what_goes_on_the_wire():
    pinned = WF2.model_copy(update={"model": "jev-1.13.0"})
    assert build_wire_request(pinned, {"state": STATE.to_dict(), "features": FEAT.to_dict()})["model"] == "jev-1.13.0"
