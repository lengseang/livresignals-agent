"""V2-2: workflow definition + policy engine. Every test asserts a concrete outcome."""
import copy
import json
import math

import pytest
from pydantic import ValidationError

from trading.v2.contracts import Decision, DecisionState, FALLBACK_STRATEGIES, tier_for_confidence
from trading.v2.ids import derive_id
from trading.v2.policy import (ExitParams, PolicyConfig, evaluate_policy, propose_trade)
from trading.v2.workflow import (ACTIONS, REQUIRED_QUESTIONS, WORKFLOW_DIR, JevWorkflow,
                                 load_active, load_workflow)

from test_v2_contracts import decision_state, fallback_decision, jev_decision, mutated

EP = ExitParams(stop_pct=0.02, target_pct=0.04)


def jev(**ch):
    d = jev_decision()
    sig = ch.pop("signals", {})
    d["signals"].update(sig)
    d.update(ch)
    return Decision.from_dict(d)


def state(**position):
    d = decision_state()
    if position:
        d["position"].update(position)
    return DecisionState.from_dict(d)


# ----------------------------------------------------------------- workflow

def test_active_workflow_loads_and_matches_decision_vocabulary():
    w = load_active()
    assert w.tag == "crypto_trading-v1"
    assert set(w.questions) == set(REQUIRED_QUESTIONS)
    assert tuple(sorted(w.questions["action"].options)) == tuple(sorted(ACTIONS))
    # the tag is a legal Decision.workflow_version
    assert Decision.from_dict(mutated(jev_decision, workflow_version=w.tag)).workflow_version == w.tag


def _wf():
    return json.loads((WORKFLOW_DIR / "crypto_trading_v1.json").read_text())


@pytest.mark.parametrize("mut,msg", [
    (lambda w: w["questions"].pop("entry_quality"), "entry_quality"),
    (lambda w: w["questions"].__setitem__("entry_quality", {"type": "choice", "options": ["A", "B"]}), "score"),
    (lambda w: w["questions"]["action"].__setitem__("options", ["LONG", "SHORT"]), "action options"),
    (lambda w: w["questions"]["risk_level"]["options"].append("CATASTROPHIC"), "risk_level options"),
    (lambda w: w["questions"]["market_regime"].__setitem__("options", ["A", "A"]), "duplicate"),
    (lambda w: w.__setitem__("version", ""), "version"),
    (lambda w: w.__setitem__("surprise", 1), "surprise"),
])
def test_workflow_that_cannot_fill_a_decision_is_rejected(mut, msg):
    w = copy.deepcopy(_wf()); mut(w)
    with pytest.raises(ValidationError, match=msg):
        JevWorkflow.model_validate(w)


def test_load_active_follows_the_pointer(tmp_path):
    (tmp_path / "v9.json").write_text(json.dumps({**_wf(), "version": "v9"}))
    (tmp_path / "active.json").write_text('{"workflow": "v9"}')
    assert load_workflow(tmp_path / "v9.json").tag == "crypto_trading-v9"
    assert load_active(tmp_path).version == "v9"


def test_derive_id_is_deterministic_and_matches_contract_patterns():
    a, b = derive_id("tp", "dec_X", "2.0.0"), derive_id("tp", "dec_X", "2.0.0")
    assert a == b != derive_id("tp", "dec_Y", "2.0.0")
    from trading.v2.contracts import TradeProposal
    assert TradeProposal.model_fields["proposal_id"].metadata  # id is pattern-constrained
    assert a.startswith("tp_") and len(a) == 3 + 26


# ----------------------------------------------------------------- policy tiers

@pytest.mark.parametrize("conf,tier", [(0.30, "IGNORE"), (0.60, "WATCH"), (0.80, "STRATEGY_CHECK"),
                                       (0.95, "EXECUTE"), (0.90, "EXECUTE"), (0.8999, "STRATEGY_CHECK")])
def test_clean_jev_decision_lands_on_its_ladder_tier(conf, tier):
    r = evaluate_policy(jev(confidence=conf))
    assert r.policy.tier == tier and r.reasons == ()
    assert r.policy.adjusted_confidence == conf
    assert (r.policy.escalate_to == "hermes") == (tier == "STRATEGY_CHECK")


def test_fallback_can_never_execute_whatever_its_confidence():
    for conf in (0.0, 0.5, 0.89, 0.8999):
        r = evaluate_policy(Decision.from_dict(mutated(fallback_decision, confidence=conf)))
        assert r.policy.tier != "EXECUTE"
    # and even if the contract's own ceiling were bypassed, policy still caps it:
    forged = Decision.model_construct(**{**Decision.from_dict(fallback_decision()).__dict__, "confidence": 0.99})
    r = evaluate_policy(forged)
    assert r.policy.tier == "STRATEGY_CHECK"
    assert "fallback_capped_below_execute" in r.reasons
    assert r.policy.adjusted_confidence < 0.90


@pytest.mark.parametrize("action", ["HOLD", "NO_TRADE"])
def test_confident_non_actionable_decision_is_ignored(action):
    r = evaluate_policy(jev(action=action, confidence=0.97))
    assert r.policy.tier == "IGNORE"
    assert f"non_actionable_{action.lower()}" in r.reasons


def test_weak_entry_quality_demotes_execute_to_strategy_check():
    r = evaluate_policy(jev(confidence=0.95, signals={"entry_quality": 0.60}))
    assert r.policy.tier == "STRATEGY_CHECK" and r.policy.escalate_to == "hermes"
    assert r.reasons[0].startswith("entry_quality_0.60_below_0.70")
    ok = evaluate_policy(jev(confidence=0.95, signals={"entry_quality": 0.70}))
    assert ok.policy.tier == "EXECUTE"  # boundary: 0.70 is acceptable


def test_weak_entry_quality_never_raises_a_low_tier():
    r = evaluate_policy(jev(confidence=0.60, signals={"entry_quality": 0.10}))
    assert r.policy.tier == "WATCH" and r.reasons == ()  # cap above current value: no-op


def test_extreme_risk_demotes_to_watch_and_other_levels_do_not():
    r = evaluate_policy(jev(confidence=0.97, risk_level="EXTREME"))
    assert r.policy.tier == "WATCH" and "risk_level_extreme" in r.reasons
    for level in ("LOW", "MEDIUM", "HIGH"):
        assert evaluate_policy(jev(confidence=0.97, risk_level=level)).policy.tier == "EXECUTE"


def test_policy_never_exceeds_decision_confidence():
    for conf in (0.0, 0.3, 0.55, 0.74, 0.9, 1.0):
        for kw in ({}, {"risk_level": "EXTREME"}, {"action": "HOLD"}, {"signals": {"entry_quality": 0.1}}):
            assert evaluate_policy(jev(confidence=conf, **kw)).policy.adjusted_confidence <= conf


def test_config_is_honoured_and_version_recorded():
    cfg = PolicyConfig(policy_version="9.9", min_entry_quality=0.90, blocked_risk_levels=("HIGH", "EXTREME"))
    r = evaluate_policy(jev(confidence=0.95, signals={"entry_quality": 0.85}), cfg)
    assert r.policy.policy_version == "9.9" and r.policy.tier == "STRATEGY_CHECK"
    assert evaluate_policy(jev(confidence=0.95, risk_level="HIGH"), cfg).policy.tier == "WATCH"


def test_policy_is_deterministic_1000x():
    d = jev(confidence=0.93, risk_level="HIGH")
    first = evaluate_policy(d).policy.to_json()
    assert all(evaluate_policy(d).policy.to_json() == first for _ in range(1000))


# ----------------------------------------------------------------- proposals

def run(decision, st=None, ep=EP, cfg=PolicyConfig()):
    st = st or state()
    return propose_trade(decision, evaluate_policy(decision, cfg), st, ep)


def test_execute_long_on_flat_book_proposes_a_sizeless_entry():
    r = run(jev(confidence=0.95))
    p = r.proposal
    assert r.reason == "proposed" and p.intent == "entry" and p.side == "LONG"
    assert (p.stop_pct, p.target_pct) == (0.02, 0.04)
    assert p.reference_price == 108420.5 and p.rationale.policy_tier == "EXECUTE"
    assert p.strategy.name == "btc_breakout_v3"
    assert "size" not in p.to_dict()


def test_proposal_id_is_deterministic():
    d = jev(confidence=0.95)
    assert run(d).proposal.proposal_id == run(d).proposal.proposal_id


@pytest.mark.parametrize("conf,reason", [(0.95, None), (0.80, "tier_strategy_check"),
                                         (0.60, "tier_watch"), (0.20, "tier_ignore")])
def test_only_execute_tier_proposes(conf, reason):
    r = run(jev(confidence=conf))
    assert (r.proposal is not None) == (reason is None)
    if reason:
        assert r.reason == reason


def test_fallback_decision_never_yields_a_proposal():
    for conf in (0.0, 0.5, 0.89):
        assert run(Decision.from_dict(mutated(fallback_decision, confidence=conf))).proposal is None


def test_missing_exit_params_means_no_entry_not_a_guess():
    r = run(jev(confidence=0.95), ep=None)
    assert r.proposal is None and r.reason == "exit_params_unknown"


@pytest.mark.parametrize("position,action,reason", [
    (dict(side="long", size=1.0, entry_price=100000.0), "LONG", "already_long"),
    (dict(side="short", size=1.0, entry_price=100000.0), "SHORT", "already_short"),
    (dict(side="long", size=1.0, entry_price=100000.0), "SHORT", "opposite_position_exit_first"),
    (dict(side="short", size=1.0, entry_price=100000.0), "LONG", "opposite_position_exit_first"),
    (dict(), "EXIT", "no_position_to_reduce"),
    (dict(), "REDUCE", "no_position_to_reduce"),
    (dict(), "HOLD", "tier_ignore"),
])
def test_position_aware_refusals(position, action, reason):
    d = jev(action=action, confidence=0.95)
    r = run(d, state(**position))
    assert r.proposal is None and r.reason == reason


@pytest.mark.parametrize("action,intent", [("EXIT", "exit"), ("REDUCE", "reduce")])
@pytest.mark.parametrize("side", ["long", "short"])
def test_exit_and_reduce_target_the_existing_position_without_stop_or_target(action, intent, side):
    r = run(jev(action=action, confidence=0.95), state(side=side, size=1.0, entry_price=100000.0))
    p = r.proposal
    assert (p.intent, p.side, p.stop_pct, p.target_pct) == (intent, side.upper(), None, None)


def test_short_entry_on_flat_book():
    p = run(jev(action="SHORT", confidence=0.95)).proposal
    assert (p.intent, p.side) == ("entry", "SHORT")


def test_mismatched_inputs_refuse_to_propose():
    d = jev(confidence=0.95)
    other_state = DecisionState.from_dict(mutated(decision_state, state_id="st_OTHER"))
    r = propose_trade(d, evaluate_policy(d), other_state, EP)
    assert r.proposal is None and r.reason == "mismatched_decision_policy_state"
    other_dec = jev(confidence=0.95, decision_id="dec_OTHER")
    r = propose_trade(d, evaluate_policy(other_dec), state(), EP)
    assert r.reason == "mismatched_decision_policy_state"


def test_choice_criteria_must_describe_exactly_the_options_and_wire_readiness_is_checked():
    base = json.loads((WORKFLOW_DIR / "crypto_trading_v2.json").read_text())
    JevWorkflow.model_validate(base).require_wire_ready()
    for mutate in (lambda w: w["questions"]["risk_level"]["criteria"].pop("EXTREME"),
                   lambda w: w["questions"]["risk_level"]["criteria"].__setitem__("CATASTROPHIC", "x")):
        w = copy.deepcopy(base); mutate(w)
        with pytest.raises(ValidationError, match="criteria keys must equal options"):
            JevWorkflow.model_validate(w)
    w = copy.deepcopy(base); w["questions"]["entry_quality"]["instructions"] = "   "
    with pytest.raises(ValueError, match="no instructions"):
        JevWorkflow.model_validate(w).require_wire_ready()
    w = copy.deepcopy(base); del w["questions"]["action"]["criteria"]
    with pytest.raises(ValueError, match="no criteria"):
        JevWorkflow.model_validate(w).require_wire_ready()
