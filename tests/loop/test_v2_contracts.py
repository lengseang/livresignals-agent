"""Stage 1 tests for the V2 contracts. Every test asserts a concrete outcome."""
import ast
import copy
import json
import pathlib

import pytest
from pydantic import ValidationError

from trading.v2 import contracts as c
from trading.v2.contracts import (
    ALL_CONTRACTS, Decision, DecisionState, ExecutionCommand, ExecutionRefused,
    FeatureSnapshot, HermesProposal, MarketEvent, PolicyDecision, RiskDecision,
    TradeProposal, UnusableSnapshot, HaltRequest,
)

ULID = "01J8ZABCDEFGH"


# ------------------------------------------------------------------ fixtures

def market_event():
    return {
        "contract": "market_event", "version": "1.0", "event_id": f"mkt_{ULID}",
        "symbol": "BTCUSDT", "venue": "binance",
        "event_time_utc": "2026-10-04T11:15:00.000Z",
        "received_time_utc": "2026-10-04T11:15:00.120Z",
        "source": "ws", "kind": "trade", "price": 108420.5, "size": 0.014,
        "bid": 108420.0, "ask": 108420.8, "spread_bps": 1.2,
    }


def feature_snapshot():
    return {
        "contract": "feature_snapshot", "version": "1.0", "snapshot_id": f"feat_{ULID}",
        "feature_version": "features-v3", "symbol": "BTCUSDT",
        "as_of_utc": "2026-10-04T11:15:00.000Z",
        "returns": {"1m": 0.0012, "5m": 0.0041, "15m": 0.0078, "1h": 0.012, "4h": 0.027},
        "technical": {"rsi": 67.8, "atr": 0.014, "ema_fast_slope": 0.003,
                      "ema_slow_slope": 0.002, "distance_vwap": 0.008},
        "volume": {"volume_1m": 1823.2, "volume_zscore": 2.71},
        "orderbook": {"imbalance": 0.31, "bid_depth": 12.8, "ask_depth": 9.7},
        "regime": {"trend": "bullish", "volatility": "high", "liquidity": "normal"},
        "data_quality": {"complete": True, "missing_fields": [], "stale_ms": 120},
    }


def decision_state():
    return {
        "contract": "decision_state", "version": "1.0", "state_id": f"st_{ULID}",
        "timestamp": 1791083882, "symbol": "BTCUSDT",
        "market": {"price": 108420.5, "spread_bps": 1.2},
        "features": f"feat_{ULID}",
        "regime": {"trend": "bullish", "volatility": "high", "liquidity": "normal"},
        "position": {"side": "none", "size": 0, "entry_price": None, "unrealized_pnl": 0},
        "risk": {"equity": 10000, "daily_pnl": -72,
                 "daily_loss_limit_remaining": 428, "current_exposure": 0.0},
        "strategy": {"name": "btc_breakout_v3", "version": "3.2.1"},
    }


HASH = "sha256:" + "ab" * 32


def jev_decision():
    return {
        "contract": "decision", "version": "1.0", "decision_id": f"dec_{ULID}",
        "state_id": f"st_{ULID}", "state_hash": HASH, "source": "jev",
        "fallback_reason": None, "fallback_strategy": None,
        "model": "jev-latest", "workflow_version": "breakout-v2",
        "action": "LONG", "confidence": 0.82,
        "probabilities": {"LONG": 0.84, "SHORT": 0.05, "HOLD": 0.06,
                          "EXIT": 0.01, "REDUCE": 0.01, "NO_TRADE": 0.03},
        "signals": {"trend_quality": 0.91, "entry_quality": 0.81,
                    "expected_edge": 0.76, "timing_quality": 0.73},
        "risk_level": "MEDIUM",
        "questions": {k: {"answer": a, "probability": p} for k, a, p in [
            ("market_regime", "high_volatility_uptrend", 0.79),
            ("trend_quality", "strong", 0.84), ("entry_quality", "good", 0.77),
            ("breakout_probability", "likely", 0.68),
            ("reversal_probability", "unlikely", 0.19),
            ("risk_level", "moderate", 0.61)]},
        "latency_ms": 240,
    }


def fallback_decision():
    return {
        "contract": "decision", "version": "1.0", "decision_id": f"dec_{ULID}",
        "state_id": f"st_{ULID}", "state_hash": HASH, "source": "fallback",
        "fallback_reason": "timeout", "fallback_strategy": "trend_follower",
        "model": None, "workflow_version": None, "action": "LONG", "confidence": 0.7,
        "probabilities": None, "signals": None, "risk_level": None, "questions": None,
        "latency_ms": 5000,
    }


def policy_decision():
    return {
        "contract": "policy_decision", "version": "1.0", "decision_id": f"dec_{ULID}",
        "policy_version": "1.4", "tier": "STRATEGY_CHECK",
        "thresholds_applied": {"ignore_below": 0.55, "watch_below": 0.75,
                               "strategy_check_below": 0.90, "execute_at_or_above": 0.90},
        "adjusted_confidence": 0.82, "escalate_to": None, "action": "STRATEGY_CHECK",
    }


def trade_proposal():
    return {
        "contract": "trade_proposal", "version": "1.0", "proposal_id": f"tp_{ULID}",
        "symbol": "BTCUSDT", "side": "LONG",
        "strategy": {"name": "btc_breakout_v3", "version": "3.2.1"},
        "intent": "entry", "reference_price": 108420.5, "stop_pct": 0.02, "target_pct": 0.04,
        "rationale": {"decision_id": f"dec_{ULID}", "policy_tier": "EXECUTE"},
    }


def checks(failed=()):
    details = {
        "daily_loss_limit": "daily_pnl -72 vs cap -1.00", "max_position_size": "1.2% of equity",
        "max_leverage": "1x", "spread_ceiling": "1.2 bps vs 5.0 cap",
        "duplicate_order": "no matching open order", "exchange_health": "REST 200, 91ms",
        "kill_switch": "not engaged",
    }
    return [{"name": n, "passed": n not in failed, "detail": d} for n, d in details.items()]


def risk_decision_approved():
    return {
        "contract": "risk_decision", "version": "1.0", "risk_decision_id": f"rd_{ULID}",
        "risk_version": "risk-v7", "proposal_id": f"tp_{ULID}", "approved": True,
        "checks": checks(), "size": 0.00092, "stop_price": 106900.0, "reject_reason": None,
    }


def risk_decision_rejected():
    d = risk_decision_approved()
    d.update(approved=False, checks=checks(failed=("daily_loss_limit",)), size=None,
             stop_price=None, reject_reason="daily_loss_limit")
    return d


def execution_command():
    return {
        "contract": "execution_command", "version": "1.0", "command_id": f"ex_{ULID}",
        "proposal_id": f"tp_{ULID}", "risk_decision_id": f"rd_{ULID}", "venue": "binance",
        "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "limit_price": None,
        "size": 0.00092, "stop_price": 106900.0, "position_side": "LONG",
        "reduce_only": False, "idempotency_key": f"tp_{ULID}-v1",
    }


def hermes_proposal():
    return {
        "contract": "hermes_proposal", "version": "1.0", "proposal_id": "prop_0182",
        "type": "MODIFY_STRATEGY", "target": "btc_breakout_v3",
        "change": {"entry_probability": 0.82, "regime_filter": "high_volatility_excluded"},
        "evidence": {"backtest_trades": 8421, "sharpe_delta": 0.31, "max_drawdown_delta": -0.04},
        "status": "proposed",
    }


def halt_request():
    return {"contract": "halt_request", "version": "1.0", "request_id": f"hr_{ULID}", "requested_by": "hermes",
            "action": "HALT_STRATEGY", "target": "btc_breakout_v3",
            "reason": "Detected abnormal execution behavior", "confidence": 0.96}


FIXTURES = {
    "market_event": market_event, "feature_snapshot": feature_snapshot,
    "decision_state": decision_state, "decision": jev_decision,
    "policy_decision": policy_decision, "trade_proposal": trade_proposal,
    "risk_decision": risk_decision_approved, "execution_command": execution_command,
    "hermes_proposal": hermes_proposal, "halt_request": halt_request,
}


def mutated(factory, **changes):
    d = factory()
    d.update(changes)
    return d


def rejects(model, data, match=None):
    with pytest.raises(ValidationError, match=match):
        model.from_dict(data)


# ------------------------------------------------- generic, all nine contracts

def test_registry_covers_ten_contracts_and_fixtures():
    assert len(ALL_CONTRACTS) == 10          # A-I from contracts.md + J (halt request, from the Notion note)
    assert set(FIXTURES) == set(ALL_CONTRACTS)


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_valid_fixture_parses_and_round_trips_exactly(name):
    model = ALL_CONTRACTS[name]
    obj = model.from_dict(FIXTURES[name]())
    assert obj.contract == name
    as_json = obj.to_json()
    again = model.from_json(as_json)
    assert again == obj
    assert again.to_json() == as_json
    assert model.from_dict(obj.to_dict()) == obj
    assert json.loads(as_json)["contract"] == name


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_unknown_field_rejected(name):
    data = FIXTURES[name]()
    data["surprise"] = 1
    rejects(ALL_CONTRACTS[name], data, "surprise")


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_wrong_contract_tag_or_version_rejected(name):
    model = ALL_CONTRACTS[name]
    rejects(model, mutated(FIXTURES[name], contract="something_else"), "contract")
    rejects(model, mutated(FIXTURES[name], version="2.0"), "version")
    data = FIXTURES[name]()
    del data["version"]
    rejects(model, data, "version")


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_models_are_immutable(name):
    obj = ALL_CONTRACTS[name].from_dict(FIXTURES[name]())
    with pytest.raises(ValidationError):
        obj.version = "9.9"


@pytest.mark.parametrize("bad", ['"high"', '"0.8"', "true", "NaN", "Infinity", "null"])
def test_confidence_is_never_coerced(bad):
    text = json.dumps(jev_decision()).replace('"confidence": 0.82', f'"confidence": {bad}')
    assert f'"confidence": {bad}' in text
    with pytest.raises(ValidationError):
        Decision.from_json(text)


def test_module_is_pure_no_io_imports():
    tree = ast.parse(pathlib.Path(c.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    forbidden = {"socket", "requests", "httpx", "aiohttp", "sqlite3", "subprocess",
                 "urllib", "http", "os", "pathlib", "logging", "time", "random", "asyncio"}
    assert imported.isdisjoint(forbidden), imported & forbidden


# ------------------------------------------------------------------ Contract A

def test_a_trade_requires_price_and_size():
    d = market_event(); d["size"] = None
    rejects(MarketEvent, d, "size")


def test_a_book_requires_bid_ask_and_rejects_crossed():
    d = mutated(market_event, kind="book", price=None, size=None)
    assert MarketEvent.from_dict(d).kind == "book"
    rejects(MarketEvent, mutated(lambda: d, bid=108421.0, ask=108420.0), "crossed")
    rejects(MarketEvent, mutated(lambda: d, ask=None), "ask")


def test_a_kline_closed_requires_timeframe_and_only_there():
    d = mutated(market_event, kind="kline_closed", size=None, timeframe="1m", sequence=18273612)
    assert MarketEvent.from_dict(d).timeframe == "1m"
    d2 = copy.deepcopy(d); d2["timeframe"] = None
    rejects(MarketEvent, d2, "timeframe")
    rejects(MarketEvent, mutated(market_event, timeframe="1m"), "only valid")
    rejects(MarketEvent, mutated(lambda: d, timeframe="soon"), "timeframe")


@pytest.mark.parametrize("field,val", [
    ("event_time_utc", "2026-10-04 11:15:00"), ("received_time_utc", "2026-13-04T11:15:00Z"),
    ("event_time_utc", "2026-10-04T11:15:00+00:00"), ("event_id", "evt_123"),
    ("symbol", "btc-usdt"), ("venue", "kraken"), ("source", "carrier_pigeon"),
    ("price", 0), ("price", -1.0), ("spread_bps", -0.1), ("price", "108420.5"),
])
def test_a_malformed_fields_rejected(field, val):
    rejects(MarketEvent, mutated(market_event, **{field: val}))


def test_a_feed_latency_is_computed_and_negative_is_allowed():
    assert MarketEvent.from_dict(market_event()).feed_latency_ms == pytest.approx(120.0)
    d = mutated(market_event, received_time_utc="2026-10-04T11:14:59.900Z")
    assert MarketEvent.from_dict(d).feed_latency_ms == pytest.approx(-100.0)


# ------------------------------------------------------------------ Contract B

def test_b_returns_must_have_exact_horizons():
    d = feature_snapshot(); del d["returns"]["4h"]
    rejects(FeatureSnapshot, d, "returns")
    d = feature_snapshot(); d["returns"]["2h"] = 0.1
    rejects(FeatureSnapshot, d, "returns")


@pytest.mark.parametrize("path,val", [
    (("technical", "rsi"), 101), (("technical", "rsi"), -1), (("technical", "atr"), -0.1),
    (("orderbook", "imbalance"), 1.5), (("orderbook", "bid_depth"), -1),
    (("volume", "volume_1m"), -5), (("regime", "trend"), "Bullish!"),
    (("data_quality", "stale_ms"), -1), (("technical", "rsi"), "67.8"),
])
def test_b_range_and_type_violations_rejected(path, val):
    d = feature_snapshot(); d[path[0]][path[1]] = val
    rejects(FeatureSnapshot, d)


def test_b_data_quality_must_be_self_consistent():
    d = feature_snapshot(); d["data_quality"]["missing_fields"] = ["rsi"]
    rejects(FeatureSnapshot, d, "complete=true")
    d = feature_snapshot(); d["data_quality"]["complete"] = False
    rejects(FeatureSnapshot, d, "complete=false")


def test_b_incomplete_or_stale_snapshot_is_unusable_not_weak():
    ok = FeatureSnapshot.from_dict(feature_snapshot())
    ok.require_usable(max_stale_ms=500)  # no raise
    with pytest.raises(UnusableSnapshot, match="stale"):
        ok.require_usable(max_stale_ms=100)
    d = feature_snapshot()
    d["data_quality"].update(complete=False, missing_fields=["orderbook.imbalance"])
    with pytest.raises(UnusableSnapshot, match="incomplete"):
        FeatureSnapshot.from_dict(d).require_usable(max_stale_ms=10_000)


def test_b_feature_version_required():
    d = feature_snapshot(); del d["feature_version"]
    rejects(FeatureSnapshot, d, "feature_version")


# ------------------------------------------------------------------ Contract C

@pytest.mark.parametrize("key", ["api_key", "api_secret", "account_id", "endpoint", "credentials"])
def test_c_state_cannot_carry_credentials_or_endpoints(key):
    d = decision_state(); d[key] = "x"
    rejects(DecisionState, d, key)
    d = decision_state(); d["risk"][key] = "x"
    rejects(DecisionState, d, key)


def test_c_position_flat_or_open_consistency():
    d = decision_state(); d["position"]["size"] = 0.5
    rejects(DecisionState, d, "side=none")
    d = decision_state(); d["position"].update(side="long", size=0.5, entry_price=None)
    rejects(DecisionState, d, "entry_price")
    d = decision_state(); d["position"].update(side="long", size=0.5, entry_price=100000.0)
    assert DecisionState.from_dict(d).position.side == "long"


def test_c_state_hash_is_deterministic_and_content_sensitive():
    a = DecisionState.from_dict(decision_state())
    b = DecisionState.from_dict(decision_state())
    assert a.state_hash() == b.state_hash()
    assert a.state_hash().startswith("sha256:") and len(a.state_hash()) == 7 + 64
    other = decision_state(); other["market"]["price"] = 108421.0
    assert DecisionState.from_dict(other).state_hash() != a.state_hash()
    assert Decision.from_dict(mutated(jev_decision, state_hash=a.state_hash())).state_hash == a.state_hash()


def test_c_features_is_a_snapshot_reference_not_embedded():
    rejects(DecisionState, mutated(decision_state, features={"rsi": 1}))
    rejects(DecisionState, mutated(decision_state, features="snapshot-1"))


# ------------------------------------------------------------------ Contract D

def test_d_jev_requires_all_typed_fields():
    for f in ("model", "workflow_version", "probabilities", "signals", "risk_level", "questions"):
        rejects(Decision, mutated(jev_decision, **{f: None}), f)


def test_d_jev_must_not_carry_fallback_fields():
    rejects(Decision, mutated(jev_decision, fallback_reason="timeout"), "fallback")
    rejects(Decision, mutated(jev_decision, fallback_strategy="tsmom"), "fallback")


@pytest.mark.parametrize("field,val", [
    ("action", "BUY"), ("action", "FLAT"), ("action", "long"), ("confidence", 1.01),
    ("confidence", -0.1), ("risk_level", "moderate"), ("state_hash", "abc"),
    ("source", "oracle"), ("latency_ms", -1),
])
def test_d_invalid_values_rejected(field, val):
    rejects(Decision, mutated(jev_decision, **{field: val}))


@pytest.mark.parametrize("action", ["LONG", "SHORT", "HOLD", "EXIT", "REDUCE", "NO_TRADE"])
def test_d_full_action_vocabulary_accepted(action):
    assert Decision.from_dict(mutated(jev_decision, action=action)).action == action


def test_d_probabilities_need_all_six_actions_each_in_unit_range():
    d = jev_decision(); del d["probabilities"]["NO_TRADE"]
    rejects(Decision, d, "NO_TRADE")
    d = jev_decision(); d["probabilities"]["LONG"] = 1.2
    rejects(Decision, d)


def test_d_signals_and_questions_are_complete_and_bounded():
    d = jev_decision(); del d["signals"]["entry_quality"]
    rejects(Decision, d, "entry_quality")
    d = jev_decision(); d["questions"]["risk_level"]["probability"] = 2
    rejects(Decision, d)
    d = jev_decision(); d["questions"]["risk_level"]["answer"] = ""
    rejects(Decision, d)
    d = jev_decision(); del d["questions"]["reversal_probability"]
    rejects(Decision, d, "reversal_probability")


def test_d_fallback_is_labelled_and_capped_below_execute():
    f = Decision.from_dict(fallback_decision())
    assert f.source == "fallback" and f.fallback_reason and f.confidence < 0.90
    rejects(Decision, mutated(fallback_decision, fallback_reason=None), "fallback_reason")
    rejects(Decision, mutated(fallback_decision, fallback_reason=""), "fallback_reason")
    rejects(Decision, mutated(fallback_decision, fallback_strategy=None), "fallback_strategy")
    rejects(Decision, mutated(fallback_decision, fallback_strategy="my_new_idea"), "fallback_strategy")
    rejects(Decision, mutated(fallback_decision, confidence=0.90), "EXECUTE")
    rejects(Decision, mutated(fallback_decision, confidence=0.99), "EXECUTE")
    assert Decision.from_dict(mutated(fallback_decision, confidence=0.8999)).confidence == 0.8999


@pytest.mark.parametrize("name", ["trend_follower", "donchian_ensemble", "tsmom", "ctrend"])
def test_d_all_four_named_fallback_strategies_accepted(name):
    assert Decision.from_dict(mutated(fallback_decision, fallback_strategy=name))


def test_d_fallback_must_not_pose_as_jev():
    d = fallback_decision(); d["model"] = "jev-latest"
    rejects(Decision, d, "Jev fields")
    d = fallback_decision(); d["signals"] = jev_decision()["signals"]
    rejects(Decision, d, "Jev fields")


def test_d_fallback_ceiling_is_not_above_default_execute_threshold():
    t = PolicyDecision.from_dict(policy_decision()).thresholds_applied
    assert c.FALLBACK_CONFIDENCE_CEILING <= t.execute_at_or_above


# ------------------------------------------------------------------ Contract E

@pytest.mark.parametrize("conf,tier", [
    (0.0, "IGNORE"), (0.549, "IGNORE"), (0.55, "WATCH"), (0.749, "WATCH"),
    (0.75, "STRATEGY_CHECK"), (0.899, "STRATEGY_CHECK"), (0.90, "EXECUTE"), (1.0, "EXECUTE"),
])
def test_e_tier_boundaries_are_lower_bounds(conf, tier):
    d = mutated(policy_decision, adjusted_confidence=conf, tier=tier, action=tier)
    assert PolicyDecision.from_dict(d).tier == tier


@pytest.mark.parametrize("conf,wrong", [(0.82, "EXECUTE"), (0.95, "STRATEGY_CHECK"),
                                        (0.30, "WATCH"), (0.60, "IGNORE")])
def test_e_tier_inconsistent_with_confidence_rejected(conf, wrong):
    rejects(PolicyDecision, mutated(policy_decision, adjusted_confidence=conf, tier=wrong,
                                    action=wrong), "inconsistent")


def test_e_action_must_equal_tier_and_escalation_only_on_strategy_check():
    rejects(PolicyDecision, mutated(policy_decision, action="EXECUTE"), "action")
    ok = mutated(policy_decision, escalate_to="hermes")
    assert PolicyDecision.from_dict(ok).escalate_to == "hermes"
    d = mutated(policy_decision, adjusted_confidence=0.95, tier="EXECUTE", action="EXECUTE",
                escalate_to="hermes")
    rejects(PolicyDecision, d, "escalate_to")


def test_e_thresholds_must_be_ordered_and_gapless():
    for bad in ({"ignore_below": 0.8}, {"watch_below": 0.5}, {"strategy_check_below": 0.85},
                {"execute_at_or_above": 1.2}, {"ignore_below": 0}):
        d = policy_decision(); d["thresholds_applied"].update(bad)
        rejects(PolicyDecision, d)


def test_e_policy_version_required():
    d = policy_decision(); del d["policy_version"]
    rejects(PolicyDecision, d, "policy_version")


# ------------------------------------------------------------------ Contract F

@pytest.mark.parametrize("key", ["size", "quantity", "qty", "notional", "amount", "leverage"])
def test_f_proposal_cannot_carry_a_size(key):
    d = trade_proposal(); d[key] = 0.5
    rejects(TradeProposal, d, key)
    assert not any(k in TradeProposal.model_fields for k in ("size", "quantity", "qty", "notional"))


def test_f_entry_requires_stop_target_and_execute_tier():
    rejects(TradeProposal, mutated(trade_proposal, stop_pct=None), "stop_pct")
    rejects(TradeProposal, mutated(trade_proposal, target_pct=None), "target_pct")
    d = trade_proposal(); d["rationale"]["policy_tier"] = "STRATEGY_CHECK"
    rejects(TradeProposal, d, "EXECUTE")


def test_f_non_entry_intents_carry_no_stop_or_target():
    for intent in ("exit", "reduce", "flatten"):
        d = mutated(trade_proposal, intent=intent, stop_pct=None, target_pct=None)
        d["rationale"]["policy_tier"] = "WATCH"
        assert TradeProposal.from_dict(d).intent == intent
        rejects(TradeProposal, mutated(lambda: d, stop_pct=0.02), "stop_pct")


@pytest.mark.parametrize("field,val", [("stop_pct", 0), ("stop_pct", 1.0), ("stop_pct", -0.1),
                                       ("side", "BUY"), ("intent", "yolo"),
                                       ("reference_price", 0), ("symbol", "btc")])
def test_f_invalid_values_rejected(field, val):
    rejects(TradeProposal, mutated(trade_proposal, **{field: val}))


# ------------------------------------------------------------------ Contract G

def test_g_approved_and_rejected_examples_parse():
    assert RiskDecision.from_dict(risk_decision_approved()).size == 0.00092
    r = RiskDecision.from_dict(risk_decision_rejected())
    assert r.size is None and r.reject_reason == "daily_loss_limit"


@pytest.mark.parametrize("missing", c.REQUIRED_RISK_CHECKS)
def test_g_every_required_check_must_be_listed(missing):
    d = risk_decision_approved()
    d["checks"] = [k for k in d["checks"] if k["name"] != missing]
    rejects(RiskDecision, d, missing)


@pytest.mark.parametrize("failing", c.REQUIRED_RISK_CHECKS)
def test_g_each_single_veto_forces_rejection(failing):
    approved_with_failure = risk_decision_approved()
    approved_with_failure["checks"] = checks(failed=(failing,))
    rejects(RiskDecision, approved_with_failure, "approved but checks failed")
    d = risk_decision_rejected()
    d["checks"] = checks(failed=(failing,)); d["reject_reason"] = failing
    r = RiskDecision.from_dict(d)
    assert not r.approved and r.reject_reason == failing


def test_g_extra_checks_from_existing_engine_are_allowed_and_enforced():
    d = risk_decision_approved()
    d["checks"].append({"name": "drawdown_halt", "passed": True, "detail": "dd -1.2% vs -15%"})
    assert len(RiskDecision.from_dict(d).checks) == 8
    d["checks"][-1]["passed"] = False
    rejects(RiskDecision, d, "approved but checks failed")


def test_g_rejection_rules():
    d = risk_decision_rejected(); d["size"] = 0.001
    rejects(RiskDecision, d, "size")
    d = risk_decision_rejected(); d["reject_reason"] = "kill_switch"
    rejects(RiskDecision, d, "reject_reason")
    d = risk_decision_rejected(); d["reject_reason"] = None
    rejects(RiskDecision, d, "reject_reason")
    d = risk_decision_rejected(); d["checks"] = checks(); 
    rejects(RiskDecision, d, "failed check")


def test_g_approval_rules_and_duplicate_check_names():
    rejects(RiskDecision, mutated(risk_decision_approved, size=None), "size")
    rejects(RiskDecision, mutated(risk_decision_approved, size=0), "size")
    rejects(RiskDecision, mutated(risk_decision_approved, reject_reason="x"), "reject_reason")
    d = risk_decision_approved(); d["checks"].append(d["checks"][0])
    rejects(RiskDecision, d, "duplicate")


def test_g_risk_version_and_id_required():
    for f in ("risk_version", "risk_decision_id"):
        d = risk_decision_approved(); del d[f]
        rejects(RiskDecision, d, f)


# ------------------------------------------------------------------ Contract H

def test_h_opening_requires_stop_and_hedge_mode_position_side():
    rejects(ExecutionCommand, mutated(execution_command, stop_price=None), "stop_price")
    rejects(ExecutionCommand, mutated(execution_command, position_side="SHORT"), "position_side")
    short = mutated(execution_command, side="SELL", position_side="SHORT")
    assert ExecutionCommand.from_dict(short).position_side == "SHORT"


def test_h_reduce_only_mapping_and_no_stop():
    close_long = mutated(execution_command, side="SELL", position_side="LONG",
                         reduce_only=True, stop_price=None)
    assert ExecutionCommand.from_dict(close_long).reduce_only
    close_short = mutated(execution_command, side="BUY", position_side="SHORT",
                          reduce_only=True, stop_price=None)
    assert ExecutionCommand.from_dict(close_short).reduce_only
    rejects(ExecutionCommand, mutated(lambda: close_long, stop_price=1.0), "stop_price")
    rejects(ExecutionCommand, mutated(lambda: close_long, side="BUY"), "position_side")


def test_h_limit_needs_price_and_market_forbids_it():
    rejects(ExecutionCommand, mutated(execution_command, type="LIMIT"), "limit_price")
    rejects(ExecutionCommand, mutated(execution_command, limit_price=100.0), "limit_price")
    ok = mutated(execution_command, type="LIMIT", limit_price=108000.0)
    assert ExecutionCommand.from_dict(ok).limit_price == 108000.0


@pytest.mark.parametrize("key", ["tp_OTHER-v1", f"tp_{ULID}", f"tp_{ULID}-v", f"tp_{ULID}-vX", ""])
def test_h_idempotency_key_bound_to_proposal(key):
    rejects(ExecutionCommand, mutated(execution_command, idempotency_key=key))


@pytest.mark.parametrize("field,val", [("risk_decision_id", None), ("risk_decision_id", "tp_1"),
                                       ("size", 0), ("size", -1), ("venue", "ftx"),
                                       ("type", "STOP")])
def test_h_invalid_or_missing_fields_rejected(field, val):
    rejects(ExecutionCommand, mutated(execution_command, **{field: val}))
    d = execution_command(); del d["risk_decision_id"]
    rejects(ExecutionCommand, d, "risk_decision_id")


def test_h_from_approved_copies_size_and_stop_from_risk_not_proposal():
    cmd = ExecutionCommand.from_approved(
        proposal=TradeProposal.from_dict(trade_proposal()),
        risk_decision=RiskDecision.from_dict(risk_decision_approved()),
        command_id=f"ex_{ULID}", venue="binance")
    assert (cmd.size, cmd.stop_price) == (0.00092, 106900.0)
    assert (cmd.side, cmd.position_side, cmd.reduce_only) == ("BUY", "LONG", False)
    assert cmd.risk_decision_id == f"rd_{ULID}" and cmd.idempotency_key == f"tp_{ULID}-v1"


def test_h_from_approved_short_entry_and_exits():
    p = trade_proposal(); p["side"] = "SHORT"
    cmd = ExecutionCommand.from_approved(
        proposal=TradeProposal.from_dict(p), risk_decision=RiskDecision.from_dict(risk_decision_approved()),
        command_id=f"ex_{ULID}", venue="binance")
    assert (cmd.side, cmd.position_side) == ("SELL", "SHORT")
    for pos_side, order_side in (("LONG", "SELL"), ("SHORT", "BUY")):
        p = mutated(trade_proposal, intent="flatten", side=pos_side, stop_pct=None, target_pct=None)
        p["rationale"]["policy_tier"] = "WATCH"
        rd = risk_decision_approved(); rd["stop_price"] = None
        cmd = ExecutionCommand.from_approved(
            proposal=TradeProposal.from_dict(p), risk_decision=RiskDecision.from_dict(rd),
            command_id=f"ex_{ULID}", venue="binance")
        assert (cmd.side, cmd.position_side, cmd.reduce_only, cmd.stop_price) == (
            order_side, pos_side, True, None)


def test_h_from_approved_refuses_unapproved_mismatched_or_unprotected():
    prop = TradeProposal.from_dict(trade_proposal())
    kw = dict(command_id=f"ex_{ULID}", venue="binance")
    with pytest.raises(ExecutionRefused, match="not approved"):
        ExecutionCommand.from_approved(
            proposal=prop, risk_decision=RiskDecision.from_dict(risk_decision_rejected()), **kw)
    other = mutated(risk_decision_approved, proposal_id="tp_DIFFERENT")
    with pytest.raises(ExecutionRefused, match="different proposal"):
        ExecutionCommand.from_approved(proposal=prop, risk_decision=RiskDecision.from_dict(other), **kw)
    unprotected = mutated(risk_decision_approved, stop_price=None)
    with pytest.raises(ExecutionRefused, match="stop_price"):
        ExecutionCommand.from_approved(
            proposal=prop, risk_decision=RiskDecision.from_dict(unprotected), **kw)


# ------------------------------------------------------------------ Contract I

def test_i_all_eight_types_accepted():
    for t in ("NEW_STRATEGY", "MODIFY_STRATEGY", "CHANGE_THRESHOLD", "CHANGE_JEV_WORKFLOW",
              "CHANGE_FEATURE", "CHANGE_POSITION_SIZING", "DISABLE_STRATEGY", "ENABLE_STRATEGY"):
        assert HermesProposal.from_dict(mutated(hermes_proposal, type=t)).type == t
    rejects(HermesProposal, mutated(hermes_proposal, type="change_strategy"))


def test_i_evidence_is_mandatory_and_must_include_a_backtest():
    d = hermes_proposal(); del d["evidence"]
    rejects(HermesProposal, d, "evidence")
    d = hermes_proposal(); d["evidence"]["backtest_trades"] = 0
    rejects(HermesProposal, d, "backtest_trades")
    d = hermes_proposal(); d["evidence"]["sharpe_delta"] = "0.31"
    rejects(HermesProposal, d, "sharpe_delta")


def test_i_proposals_are_inert():
    for status in ("approved", "live", "active", "promoted", ""):
        rejects(HermesProposal, mutated(hermes_proposal, status=status), "status")


@pytest.mark.parametrize("key", ["max_leverage", "kill_switch_enabled", "daily_loss_limit",
                                 "MAX_POSITION_USD", "live_enabled", "api_key", "withdraw_to"])
def test_i_cannot_target_risk_limits_or_secrets(key):
    d = hermes_proposal(); d["change"] = {key: 5}
    rejects(HermesProposal, d, "forbidden")


def test_i_change_must_not_be_empty_and_id_prefix_enforced():
    rejects(HermesProposal, mutated(hermes_proposal, change={}), "change")
    rejects(HermesProposal, mutated(hermes_proposal, proposal_id="hp_0182"))


# ------------------------------------------------------------------ Contract J

@pytest.mark.parametrize("action,target,ok", [
    ("HALT_ALL", None, True), ("HALT_ALL", "BTCUSDT", False),
    ("HALT_SYMBOL", "BTCUSDT", True), ("HALT_SYMBOL", None, False), ("HALT_SYMBOL", "btc", False),
    ("HALT_STRATEGY", "btc_breakout_v3", True), ("HALT_STRATEGY", None, False),
    ("REDUCE_EXPOSURE", None, True), ("REDUCE_EXPOSURE", "BTCUSDT", True),
    ("PLACE_ORDER", None, False), ("DISABLE_KILL_SWITCH", None, False), ("halt_all", None, False),
])
def test_j_halt_request_scope_rules_and_closed_action_vocabulary(action, target, ok):
    d = mutated(halt_request, action=action, target=target)
    if ok:
        assert HaltRequest.from_dict(d).action == action
    else:
        rejects(HaltRequest, d)


def test_j_only_hermes_may_request_and_reason_and_confidence_are_validated():
    rejects(HaltRequest, mutated(halt_request, requested_by="jev"))
    rejects(HaltRequest, mutated(halt_request, reason=""))
    rejects(HaltRequest, mutated(halt_request, reason="x" * 501))
    for bad in (-0.1, 1.1, "0.9", None):
        rejects(HaltRequest, mutated(halt_request, confidence=bad))
    rejects(HaltRequest, mutated(halt_request, request_id="prop_1"))
