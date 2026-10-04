"""V2-15 L2: position advice (Jev's position workflow, gateway on failure) and its risk-reducing-only authority."""
import json

import pytest

from trading.shadow.chain import ShadowChain
from trading.v2.adapter import JevAdapter, JevHttpError, JevProtocolError
from trading.v2.advisor import PositionAdvice, PositionAdvisor, PositionPolicy, normalise_position_reply, plan
from trading.v2.contracts import FALLBACK_STRATEGIES, DecisionState, FeatureSnapshot
from trading.v2.fallback import StaticSource
from trading.v2.gateway import Gateway, PositionView
from trading.v2.paper import PaperBook
from trading.v2.risk import RiskLimits
from trading.v2.shadow import ShadowPipeline
from trading.v2.typesafe_client import build_wire_request, translate_response
from trading.v2.workflow import PositionWorkflow, load_active, load_position_workflow

from test_v2_contracts import decision_state, feature_snapshot, mutated
from test_v2_features import T0
from test_v2_paper import FEE, provider
from test_v2_shadow import LONG_RAW, Client, FollowBook, make_bars

PWF = load_position_workflow()
DIST = {"HOLD": 0.1, "TIGHTEN_STOP": 0.05, "REDUCE": 0.05, "EXIT": 0.8}


def reply(action="EXIT", dist=None, conf=None, **scores):
    d = dist or DIST
    s = {"momentum_fading": 0.8, "reversal_risk": 0.7, "target_reachable": 0.2, **scores}
    return {"model": "jev-1.13.0", "answers": {
        "position_action": {"value": action, "probability": conf, "distribution": d},
        **{k: {"value": v} for k, v in s.items()}}}


VIEW = PositionView(side="LONG", entry=100000.0, stop=96500.0, target=107000.0, best=104000.0, last=103000.0,
                    minutes_held=240)


def st(side="long"):
    d = decision_state()
    d["position"] = {"side": side, "size": 0.01, "entry_price": 100000.0, "unrealized_pnl": 30.0} if side != "none" \
        else d["position"]
    return DecisionState.from_dict(d)


def snap():
    return FeatureSnapshot.from_dict(mutated(feature_snapshot, feature_version="features-v1"))


class FakeClient:
    def __init__(self, r):
        self.r, self.requests = r, []

    def evaluate(self, request, timeout_s):
        self.requests.append(request)
        if isinstance(self.r, Exception):
            raise self.r
        return json.loads(json.dumps(self.r))


def advisor(r):
    return PositionAdvisor(FakeClient(r), PWF, Gateway(), timeout_s=1.0, clock=lambda: 0.0)


# ------------------------------------------------------------ workflow + wire

def test_position_workflow_is_wire_ready_and_only_reduces_risk():
    PWF.require_wire_ready()
    assert set(PWF.questions["position_action"].options) == {"HOLD", "TIGHTEN_STOP", "REDUCE", "EXIT"}


def test_position_workflow_rejects_an_entry_action():
    d = json.loads(load_position_workflow().model_dump_json())
    d["questions"]["position_action"]["options"].append("LONG")
    d["questions"]["position_action"]["criteria"]["LONG"] = "open"
    with pytest.raises(ValueError, match="exactly"):
        PositionWorkflow.model_validate(d)


def test_wire_request_carries_the_position_and_the_feature_values():
    a = advisor(reply())
    a.advise(st(), snap(), VIEW)
    wire = build_wire_request(PWF, a.client.requests[0])
    assert wire["state"]["position_detail"]["mfe_R"] == pytest.approx(4000 / 3500, rel=1e-3)
    assert isinstance(wire["state"]["features"], dict)
    assert wire["questions"]["momentum_fading"]["type"] == "noul"


def test_wire_reply_translates_for_the_position_workflow():
    wire = {"model": "jev-1.13.0", "answers": {
        "position_action": {"type": "choice", "choice": "TIGHTEN_STOP", "probabilities": {
            "HOLD": 0.2, "TIGHTEN_STOP": 0.7, "REDUCE": 0.05, "EXIT": 0.05}, "confidence": 0.66},
        "momentum_fading": {"type": "noul", "noul": 0.71}, "reversal_risk": {"type": "noul", "noul": 0.4},
        "target_reachable": {"type": "noul", "noul": 0.3}}}
    model, action, conf, dist, scores = normalise_position_reply(translate_response(wire, PWF), PWF)
    assert (action, conf, scores["momentum_fading"]) == ("TIGHTEN_STOP", 0.66, 0.71)


# ------------------------------------------------------------ strict normalisation

@pytest.mark.parametrize("bad,cat", [
    (reply(action="LONG"), "unknown_enum"),
    (reply(dist={"HOLD": 1.0}), "schema_violation"),
    (reply(dist={"HOLD": 0.5, "TIGHTEN_STOP": 0.1, "REDUCE": 0.1, "EXIT": 0.1}), "schema_violation"),
    (reply(momentum_fading=1.5), "schema_violation"),
    (reply(momentum_fading="high"), "schema_violation"),
    ({"model": "x", "answers": {}}, "schema_violation"),
    ("nope", "malformed_json"),
])
def test_bad_replies_are_rejected(bad, cat):
    with pytest.raises(JevProtocolError) as e:
        normalise_position_reply(bad, PWF)
    assert e.value.category == cat


def test_confidence_is_the_more_conservative_of_probability_and_jev_confidence():
    assert normalise_position_reply(reply(conf=0.6), PWF)[2] == 0.6
    assert normalise_position_reply(reply(conf=None), PWF)[2] == 0.8


# ------------------------------------------------------------ advice and fallback

def test_jev_answer():
    a = advisor(reply()).advise(st(), snap(), VIEW)
    assert (a.source, a.action, a.confidence, a.fallback_reason) == ("jev", "EXIT", 0.8, None)


@pytest.mark.parametrize("r", [JevHttpError("down"), reply(action="BUY"), {"garbage": 1}])
def test_any_jev_failure_means_the_gateway_answers(r):
    a = advisor(r).advise(st(), snap(), VIEW)
    assert a.source == "gateway" and a.fallback_reason and a.action == "HOLD"


def test_advice_only_while_positioned():
    with pytest.raises(ValueError):
        advisor(reply()).advise(st("none"), snap(), VIEW)


def test_advice_record_is_validated():
    with pytest.raises(ValueError):
        PositionAdvice("adv_X", "gateway", "m", "w", "HOLD", 0.5, {}, {}, {}, 0.0)   # gateway without a reason


# ------------------------------------------------------------ plan: thresholds and bounds

def adv(action, conf):
    return PositionAdvice("adv_X", "jev", "m", "w", action, conf, {}, {}, {}, 0.0)


@pytest.mark.parametrize("action,conf,kind", [
    ("EXIT", 0.69, "hold"), ("EXIT", 0.70, "exit"), ("REDUCE", 0.7, "reduce"), ("REDUCE", 0.5, "hold"),
    ("TIGHTEN_STOP", 0.59, "hold"), ("TIGHTEN_STOP", 0.6, "tighten"), ("HOLD", 0.99, "hold"),
])
def test_plan_thresholds(action, conf, kind):
    assert plan(adv(action, conf), VIEW, 0.01, PositionPolicy()).kind == kind


def test_tighten_goes_to_last_minus_k_atr_and_never_loosens():
    p = plan(adv("TIGHTEN_STOP", 0.9), VIEW, 0.01, PositionPolicy())
    assert p.new_stop == pytest.approx(103000 * (1 - 1.5 * 0.01))
    tight = PositionView(**{**VIEW.__dict__, "stop": 102500.0})
    assert plan(adv("TIGHTEN_STOP", 0.9), tight, 0.01, PositionPolicy()).kind == "hold"
    short = PositionView(**{**VIEW.__dict__, "side": "SHORT", "stop": 106000.0, "target": 93000.0})
    assert plan(adv("TIGHTEN_STOP", 0.9), short, 0.01, PositionPolicy()).new_stop == pytest.approx(103000 * 1.015)


def test_policy_refuses_sub_coin_flip_thresholds():
    with pytest.raises(ValueError):
        PositionPolicy(exit_at=0.4)


# ------------------------------------------------------------ in the pipeline, with a paper position

def pipeline(tmp_path, pos_reply, kill=False):
    adapter = JevAdapter(Client(LONG_RAW), load_active(), StaticSource({n: "LONG" for n in FALLBACK_STRATEGIES}),
                         timeout_s=1.0, clock=lambda: 0.0)
    bk = PaperBook(10_000.0, FEE, ShadowChain(tmp_path, "v2paper", "book", T0))
    kills = {"on": False}
    pr = provider(bk, kill=False)
    pr._kill = lambda: kills["on"]
    fb = FollowBook()
    adv_ = PositionAdvisor(FakeClient(pos_reply), PWF, Gateway(), timeout_s=1.0, clock=lambda: 0.0)
    p = ShadowPipeline(adapter=adapter, provider=pr, sink=ShadowChain(tmp_path, "v2cycle", "pipeline", T0), book=fb,
                       limits=RiskLimits(daily_loss_cap_usd=100.0), paper=bk, advisor=adv_)
    fb.pipeline = p
    return p, bk, kills


def positioned_records(recs):
    return [r for r in recs if r.get("advice") is not None]


def test_jev_exit_advice_closes_the_position_through_the_risk_engine(tmp_path):
    p, bk, _ = pipeline(tmp_path, reply("EXIT"))
    recs = p.run(make_bars())
    adv_recs = positioned_records(recs)
    assert adv_recs and adv_recs[0]["outcome"] == "position_exit"
    assert adv_recs[0]["risk"]["approved"] and adv_recs[0]["execution_preview"]["reduce_only"] is True
    assert any(t["reason"] == "exit" for t in bk.trades)


def test_jev_tighten_advice_amends_the_stop(tmp_path):
    tight = {"HOLD": 0.1, "TIGHTEN_STOP": 0.8, "REDUCE": 0.05, "EXIT": 0.05}
    p, bk, _ = pipeline(tmp_path, reply("TIGHTEN_STOP", dist=tight))
    recs = positioned_records(p.run(make_bars()))
    assert recs and recs[0]["outcome"] == "position_tighten"
    assert len(bk.adjustments) >= 2 and all(a.source == "jev" for a in bk.adjustments)
    assert recs[0]["adjustment"]["new_stop"] == pytest.approx(recs[0]["position_plan"]["new_stop"])
    stops = [a.new_stop for a in bk.adjustments]
    assert stops == sorted(stops)


def test_jev_down_in_position_means_gateway_hold(tmp_path):
    p, bk, _ = pipeline(tmp_path, JevHttpError("down"))
    recs = positioned_records(p.run(make_bars()))
    assert recs and all(r["advice"]["source"] == "gateway" and r["outcome"] == "position_hold" for r in recs)
    assert p.stats["advice:gateway"] == len(recs)


def test_exit_advice_below_threshold_is_a_hold(tmp_path):
    weak = {"HOLD": 0.35, "TIGHTEN_STOP": 0.1, "REDUCE": 0.05, "EXIT": 0.5}
    p, bk, _ = pipeline(tmp_path, reply("EXIT", dist=weak))
    recs = positioned_records(p.run(make_bars()))
    assert recs and all(r["outcome"] == "position_hold" for r in recs)
    assert not any(t["reason"] == "exit" for t in bk.trades)
