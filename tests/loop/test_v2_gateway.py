"""V2-15: the System One gateway. Same questions, same road, honest about what is fitted, never EXECUTE."""
import copy
import json
import math

import pytest

from trading.v2.adapter import JevAdapter, JevHttpError
from trading.v2.contracts import FALLBACK_STRATEGIES, GATEWAY_STRATEGY, Decision, DecisionState, FeatureSnapshot
from trading.v2.fallback import StaticSource
from trading.v2.gateway import MODEL_FILE, Gateway, GatewayModel, PositionView, feature_row
from trading.v2.policy import evaluate_policy
from trading.v2.studies.history import FEATURE_COLUMNS, _row
from trading.v2.workflow import load_active

from test_v2_contracts import decision_state, feature_snapshot, mutated

WF = load_active()
DATA = json.loads(MODEL_FILE.read_text())


def snap(**over):
    d = mutated(feature_snapshot, feature_version="features-v1")
    for k, v in over.items():
        d[k] = {**d[k], **v} if isinstance(v, dict) else v
    return FeatureSnapshot.from_dict(d)


def state(position=None, spread=1.2):
    d = decision_state()
    d["market"]["spread_bps"] = spread
    if position:
        d["position"] = position
    return DecisionState.from_dict(d)


LONG_POS = {"side": "long", "size": 0.01, "entry_price": 100000.0, "unrealized_pnl": 84.2}


def confident_model():
    """A deliberately over-confident model (huge intercept): proves the CAP, not the model, keeps it from EXECUTE."""
    d = copy.deepcopy(DATA)
    d["models"]["LONG"]["w"][0] = 8.0
    d["models"]["LONG"]["shrink"] = 1.0
    return GatewayModel(d)


def decide(gw=None, st=None, f=None, **kw):
    return (gw or Gateway()).decide(st or state(), f or snap(), workflow_tag=WF.tag, reason="test", **kw)


# ------------------------------------------------------------ the model file is the evaluated one

def test_model_file_matches_the_feature_definition_and_the_evidence():
    assert tuple(DATA["columns"]) == FEATURE_COLUMNS
    ev = json.loads((MODEL_FILE.parents[3] / DATA["evidence"]).read_text())
    assert DATA["admitted_for_entries"] is ev["admission"]["passed"] is False
    assert DATA["break_even_p"] == pytest.approx(ev["break_even_p"])


def test_runtime_feature_row_equals_the_study_row():
    f = snap()
    assert feature_row(f.to_dict()) == _row(f)


def test_model_refuses_another_feature_version():
    with pytest.raises(ValueError):
        GatewayModel({**DATA, "feature_version": "features-v2"})
    with pytest.raises(ValueError, match="features-v1"):
        Gateway().decide(state(), FeatureSnapshot.from_dict(feature_snapshot()), workflow_tag=WF.tag, reason="x")


def test_features_must_belong_to_the_state():
    d = mutated(feature_snapshot, feature_version="features-v1", snapshot_id="feat_OTHER")
    with pytest.raises(ValueError, match="belong"):
        decide(f=FeatureSnapshot.from_dict(d))


# ------------------------------------------------------------ same questions, same road

def test_decision_is_a_full_gateway_decision():
    d = decide()
    assert isinstance(d, Decision)
    assert (d.source, d.fallback_strategy, d.model) == ("fallback", GATEWAY_STRATEGY, "gateway-entry-v1")
    assert d.signals is not None and d.questions is not None and d.risk_level is not None
    assert math.isclose(sum(d.probabilities.to_dict().values()), 1.0, abs_tol=1e-9)
    assert d.confidence == pytest.approx(min(d.probabilities.to_dict()[d.action], 0.89))
    assert Decision.from_json(d.to_json()) == d


def test_deterministic():
    assert decide().to_json() == decide().to_json()


def test_contract_still_forbids_jev_fields_on_a_voting_fallback():
    from pydantic import ValidationError
    d = decide().to_dict()
    d["fallback_strategy"] = "trend_follower"
    with pytest.raises(ValidationError, match="must not carry"):
        Decision.from_dict(d)


def test_contract_requires_every_answer_from_the_gateway():
    from pydantic import ValidationError
    d = decide().to_dict()
    d["signals"] = None
    with pytest.raises(ValidationError, match="gateway decision requires"):
        Decision.from_dict(d)


# ------------------------------------------------------------ authority

def test_unskilled_model_does_not_enter():
    # the fitted model has no out-of-sample skill, so with ordinary features the gateway says NO_TRADE
    assert decide().action == "NO_TRADE"


def test_even_a_confident_model_can_never_execute_an_entry():
    d = decide(gw=Gateway(confident_model()), f=snap(regime={"volatility": "normal"}))
    assert d.action == "LONG" and d.confidence <= 0.89
    assert evaluate_policy(d).policy.tier != "EXECUTE"


def test_extreme_risk_suppresses_entries():
    thin = snap(regime={"liquidity": "thin"})
    d = decide(gw=Gateway(confident_model()), f=thin)
    assert d.risk_level == "EXTREME" and d.action == "NO_TRADE"


def test_in_position_default_is_hold():
    d = decide(st=state(LONG_POS))
    assert d.action == "HOLD"


def test_in_position_disorderly_market_exits():
    d = decide(st=state(LONG_POS, spread=25.0))
    assert d.risk_level == "EXTREME" and d.action == "EXIT" and d.confidence == pytest.approx(0.72)


def test_position_answers_driftless_reach():
    gw = Gateway()
    pv = PositionView(side="LONG", entry=100.0, stop=96.5, target=107.0, best=104.0, last=103.0, minutes_held=90)
    a = gw.position_answers(snap().to_dict(), 1.2, pv)
    assert a["target_reachable"] == pytest.approx((103.0 - 96.5) / (107.0 - 96.5))
    assert set(a["position_action_distribution"]) == {"HOLD", "TIGHTEN_STOP", "REDUCE", "EXIT"}
    assert pv.as_features()["mfe_R"] == pytest.approx(4.0 / 3.5, rel=1e-3)


def test_quantile_cdf_is_monotone():
    m = GatewayModel.load()
    xs = [i * 0.0005 for i in range(0, 120)]
    ps = [m.pct("atr", x) for x in xs]
    assert ps == sorted(ps) and 0 <= ps[0] and ps[-1] <= 1


# ------------------------------------------------------------ adapter fail-over

class Down:
    def evaluate(self, request, timeout_s):
        raise JevHttpError("down")


def adapter(gw):
    return JevAdapter(Down(), WF, StaticSource({n: "LONG" for n in FALLBACK_STRATEGIES}), timeout_s=1.0,
                      clock=lambda: 0.0, gateway=gw)


def test_jev_down_means_the_gateway_answers():
    a = adapter(Gateway())
    d = a.evaluate(state(), snap())
    assert d.fallback_strategy == GATEWAY_STRATEGY and d.fallback_reason.startswith("http_error")
    assert a.stats["fallback_gateway"] == 1


def test_gateway_failure_falls_back_to_the_vote():
    class Broken(Gateway):
        def decide(self, *a, **k):
            raise RuntimeError("boom")
    a = adapter(Broken())
    d = a.evaluate(state(), snap())
    assert d.fallback_strategy in FALLBACK_STRATEGIES and "gateway_failed:RuntimeError" in d.fallback_reason
    assert a.stats["gateway_failed"] == 1


def test_no_features_means_the_vote():
    d = adapter(Gateway()).evaluate(state(), None)
    assert d.fallback_strategy in FALLBACK_STRATEGIES


def test_an_unskilled_model_says_the_base_rate():
    m = GatewayModel.load()
    x = feature_row(snap().to_dict())
    assert m.long.shrink == 0.0 and m.long.p(x) == pytest.approx(m.long.base)
    assert m.long.raw(x) != pytest.approx(m.long.base)       # the raw fit does move; it is just not trusted


# ------------------------------------------------------------ dual-run in the pipeline

def test_pipeline_dual_runs_the_gateway_when_jev_answers(tmp_path):
    from test_v2_shadow import build, make_bars
    p = build(tmp_path)
    p.adapter.gateway = Gateway()
    recs = [r for r in p.run(make_bars()) if r["outcome"] != "no_snapshot"]
    assert recs and all(r["decision"]["source"] == "jev" for r in recs)
    assert all(r["gateway_dual_run"]["decision"]["fallback_strategy"] == GATEWAY_STRATEGY for r in recs)
    assert p.stats["dual:agree"] + p.stats["dual:disagree"] == len(recs)


def test_pipeline_gateway_takes_over_when_jev_is_down(tmp_path):
    from test_v2_shadow import build, make_bars
    p = build(tmp_path, reply=JevHttpError("down"))
    p.adapter.gateway = Gateway()
    recs = [r for r in p.run(make_bars()) if r["outcome"] != "no_snapshot"]
    assert recs and all(r["decision"]["fallback_strategy"] == GATEWAY_STRATEGY for r in recs)
    assert all(r["gateway_dual_run"] is None for r in recs)
    assert p.stats["outcome:would_execute"] == 0


def test_runner_wires_the_gateway():
    import inspect
    from trading.v2 import runner
    assert "gateway=Gateway(" in inspect.getsource(runner.Runner)
