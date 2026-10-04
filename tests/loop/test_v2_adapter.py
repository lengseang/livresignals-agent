"""V2-3: the Jev adapter and its fallback. Forced timeout / error / malformed / schema violation /
unknown enum / empty balance must ALL yield source="fallback" with a reason, never a crash."""
import copy
import logging
import time

import pytest

from trading.v2.adapter import (CATEGORIES, JevAdapter, JevBalanceExhausted, JevHttpError,
                                JevMalformedJson, build_request, normalise)
from trading.v2.contracts import FALLBACK_CONFIDENCE_CEILING, FALLBACK_STRATEGIES, Decision, DecisionState
from trading.v2.fallback import FALLBACK_MAX_CONFIDENCE, StaticSource, aggregate
from trading.v2.policy import ExitParams, evaluate_policy, propose_trade
from trading.v2.workflow import load_active

from test_v2_contracts import decision_state

WF = load_active()
ALL_LONG = StaticSource({n: "LONG" for n in FALLBACK_STRATEGIES})


def state(**position):
    d = decision_state()
    if position:
        d["position"].update(position)
    return DecisionState.from_dict(d)


def raw_ok(**over):
    r = {
        "model": "jev-latest",
        "answers": {
            "market_regime": {"value": "TREND_UP", "probability": 0.91},
            "trend_quality": {"value": 0.91},
            "breakout_probability": {"value": 0.68},
            "reversal_probability": {"value": 0.19},
            "entry_quality": {"value": 0.81},
            "expected_edge": {"value": 0.76},
            "timing_quality": {"value": 0.73},
            "risk_level": {"value": "MEDIUM", "probability": 0.78},
            "action": {"value": "LONG", "probability": 0.84,
                       "distribution": {"LONG": 0.84, "SHORT": 0.05, "HOLD": 0.06,
                                        "EXIT": 0.01, "REDUCE": 0.01, "NO_TRADE": 0.03}},
        },
    }
    r.update(over)
    return r


class Client:
    def __init__(self, behaviour):
        self.behaviour, self.calls, self.requests = behaviour, 0, []

    def evaluate(self, request, timeout_s):
        self.calls += 1
        self.requests.append(request)
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        if callable(self.behaviour):
            return self.behaviour(request, timeout_s)
        return copy.deepcopy(self.behaviour)


def adapter(behaviour, fallback=ALL_LONG, timeout_s=1.0, **kw):
    return JevAdapter(Client(behaviour), WF, fallback, timeout_s=timeout_s, **kw)


def mut(path, value):
    r = raw_ok()
    node = r
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    return r


# ------------------------------------------------------------ happy path

def test_good_reply_becomes_a_typed_jev_decision():
    a = adapter(raw_ok())
    s = state()
    d = a.evaluate(s)
    assert d.source == "jev" and d.fallback_reason is None and d.fallback_strategy is None
    assert d.action == "LONG" and d.confidence == 0.84          # = probability of the chosen action
    assert d.model == "jev-latest" and d.workflow_version == "crypto_trading-v1"
    assert d.state_id == s.state_id and d.state_hash == s.state_hash()
    assert d.signals.entry_quality == 0.81 and d.risk_level == "MEDIUM"
    assert d.probabilities.LONG == 0.84 and d.questions.market_regime.answer == "TREND_UP"
    assert d.questions.trend_quality.probability == 0.91
    assert a.stats["jev"] == 1 and a.stats["fallback"] == 0 and a.stats["calls"] == 1
    assert Decision.from_json(d.to_json()) == d


def test_request_carries_state_and_workflow_but_no_credentials():
    c = Client(raw_ok()); a = JevAdapter(c, WF, ALL_LONG)
    s = state(); a.evaluate(s)
    req = c.requests[0]
    assert req["state"] == s.to_dict() and req["state_hash"] == s.state_hash()
    assert set(req["questions"]) == set(WF.questions) and req["workflow"] == "crypto_trading-v1"
    blob = repr(req).lower()
    assert not any(w in blob for w in ("api_key", "secret", "password", "token", "private"))


def test_decision_ids_are_deterministic_per_state_and_source():
    s = state()
    a, b = adapter(raw_ok()).evaluate(s), adapter(raw_ok()).evaluate(s)
    assert a.decision_id == b.decision_id
    f = adapter(JevHttpError()).evaluate(s)
    assert f.decision_id != a.decision_id and f.source == "fallback"


# ------------------------------------------------------------ every failure -> labelled fallback

FAILURES = [
    ("http_error", JevHttpError("HTTP 500 https://x?key=SECRET123")),
    ("malformed_json", JevMalformedJson("Expecting value: line 1 column 1")),
    ("balance_exhausted", JevBalanceExhausted("402 payment required")),
    ("client_error", RuntimeError("boom with token=abc")),
    ("malformed_json", "this is not an object"),
    ("malformed_json", ["a", "list"]),
    ("malformed_json", None),
    ("schema_violation", {}),
    ("schema_violation", {"model": "jev-latest"}),
    ("schema_violation", raw_ok(model="")),
    ("schema_violation", {**raw_ok(), "surprise": 1}),
    ("schema_violation", mut(["answers", "entry_quality", "value"], "high")),         # "high" is not 0.0
    ("schema_violation", mut(["answers", "entry_quality", "value"], 1.5)),
    ("schema_violation", mut(["answers", "entry_quality", "value"], -0.1)),
    ("schema_violation", mut(["answers", "entry_quality", "value"], True)),
    ("schema_violation", mut(["answers", "entry_quality", "value"], float("nan"))),
    ("schema_violation", mut(["answers", "action", "value"], 3)),
    ("schema_violation", mut(["answers", "action", "distribution"], None)),
    ("schema_violation", mut(["answers", "action", "distribution"], {"LONG": 1.0})),
    ("schema_violation", mut(["answers", "action", "distribution", "LONG"], 0.2)),     # sums to 0.38
    ("schema_violation", mut(["answers", "action", "distribution", "LONG"], 1.4)),
    ("schema_violation", mut(["answers", "risk_level", "probability"], None)),         # choice w/o probability
    ("unknown_enum", mut(["answers", "action", "value"], "BUY")),
    ("unknown_enum", mut(["answers", "action", "value"], "long")),
    ("unknown_enum", mut(["answers", "risk_level", "value"], "SPICY")),
    ("unknown_enum", mut(["answers", "market_regime", "value"], "MOON")),
]


@pytest.mark.parametrize("category,behaviour", FAILURES, ids=[f"{i}-{c}" for i, (c, _) in enumerate(FAILURES)])
def test_every_failure_yields_a_labelled_capped_fallback_never_a_crash(category, behaviour):
    a = adapter(behaviour)
    d = a.evaluate(state())
    assert d.source == "fallback"
    assert d.fallback_reason.startswith(category + ":"), d.fallback_reason
    assert d.fallback_strategy in FALLBACK_STRATEGIES
    assert d.confidence <= FALLBACK_MAX_CONFIDENCE < FALLBACK_CONFIDENCE_CEILING
    assert d.model is None and d.signals is None and d.probabilities is None
    assert a.stats["fallback"] == 1 and a.stats[f"fallback:{category}"] == 1 and a.stats["jev"] == 0


def test_missing_and_unexpected_answers_are_schema_violations():
    r = raw_ok(); del r["answers"]["timing_quality"]
    d = adapter(r).evaluate(state())
    assert d.fallback_reason.startswith("schema_violation") and "timing_quality" in d.fallback_reason
    r = raw_ok(); r["answers"]["bonus"] = {"value": 1.0}
    assert "bonus" in adapter(r).evaluate(state()).fallback_reason


def test_reasons_never_leak_exception_text_or_secrets():
    for exc in (JevHttpError("https://api?key=SECRET123"), RuntimeError("token=abc"),
                ValueError("password=hunter2")):
        reason = adapter(exc).evaluate(state()).fallback_reason
        assert not any(w in reason for w in ("SECRET123", "token=abc", "hunter2", "https://"))
        assert type(exc).__name__ in reason


def test_timeout_is_enforced_and_does_not_block_the_hot_path():
    a = adapter(lambda req, t: time.sleep(0.6) or raw_ok(), timeout_s=0.05)
    t0 = time.monotonic()
    d = a.evaluate(state())
    assert time.monotonic() - t0 < 0.4, "hung client must not hold the decision loop"
    assert d.source == "fallback" and d.fallback_reason.startswith("timeout:")
    assert a.stats["fallback:timeout"] == 1


def test_client_that_ignores_its_timeout_is_still_a_timeout():
    ticks = iter([0.0, 5.0, 5.0, 5.0])
    a = JevAdapter(Client(raw_ok()), WF, ALL_LONG, timeout_s=1.0, clock=lambda: next(ticks))
    d = a.evaluate(state())
    assert d.source == "fallback" and d.fallback_reason.startswith("timeout:") and d.latency_ms == 5000.0


def test_latency_is_recorded_on_jev_decisions():
    ticks = iter([10.0, 10.24, 10.24])
    d = JevAdapter(Client(raw_ok()), WF, ALL_LONG, timeout_s=1.0, clock=lambda: next(ticks)).evaluate(state())
    assert d.source == "jev" and d.latency_ms == pytest.approx(240.0)


def test_timeout_must_be_positive():
    for bad in (0, -1, float("nan")):
        with pytest.raises(ValueError):
            JevAdapter(Client(raw_ok()), WF, ALL_LONG, timeout_s=bad)


def test_all_declared_categories_are_exercised():
    seen = {c for c, _ in FAILURES} | {"timeout"}
    assert seen == set(CATEGORIES)


# ------------------------------------------------------------ the fallback, in isolation

def S(**kw):
    base = {n: "UNAVAILABLE" for n in FALLBACK_STRATEGIES}
    base.update(kw)
    return base


def test_unanimous_long_on_flat_book_is_capped_below_execute():
    v = aggregate(S(**{n: "LONG" for n in FALLBACK_STRATEGIES}), "none")
    assert (v.action, v.confidence) == ("LONG", 0.89) and v.reason == "consensus_long_4_of_4"
    assert v.strategy == "trend_follower"


@pytest.mark.parametrize("n_long,expected_conf", [(3, 0.75), (2, 0.5), (1, 0.25)])
def test_confidence_is_votes_over_four_missing_votes_count_against(n_long, expected_conf):
    names = list(FALLBACK_STRATEGIES)
    v = aggregate(S(**{n: "LONG" for n in names[:n_long]}), "none")
    assert v.action == "LONG" and v.confidence == expected_conf


def test_ties_and_no_votes_yield_no_action_and_zero_confidence():
    tie = aggregate(S(trend_follower="LONG", tsmom="FLAT"), "none")
    assert (tie.action, tie.confidence, tie.reason) == ("NO_TRADE", 0.0, "no_consensus")
    held = aggregate(S(trend_follower="LONG", tsmom="FLAT"), "long")
    assert held.action == "HOLD"
    none = aggregate(S(), "none")
    assert (none.action, none.confidence, none.reason) == ("NO_TRADE", 0.0, "no_strategy_available")


@pytest.mark.parametrize("winner,position,action", [
    ("LONG", "none", "LONG"), ("LONG", "long", "HOLD"), ("LONG", "short", "EXIT"),
    ("SHORT", "none", "SHORT"), ("SHORT", "short", "HOLD"), ("SHORT", "long", "EXIT"),
    ("FLAT", "none", "NO_TRADE"), ("FLAT", "long", "EXIT"), ("FLAT", "short", "EXIT"),
])
def test_stance_to_action_is_position_aware(winner, position, action):
    assert aggregate(S(trend_follower=winner, tsmom=winner, ctrend=winner), position).action == action


def test_invalid_stances_and_unknown_strategies_are_unavailable_not_trusted():
    v = aggregate({"trend_follower": "BUY!", "tsmom": None, "my_new_idea": "LONG",
                   "ctrend": "LONG", "donchian_ensemble": "LONG"}, "none")
    assert v.votes["trend_follower"] == "UNAVAILABLE" and v.votes["tsmom"] == "UNAVAILABLE"
    assert "my_new_idea" not in v.votes and v.confidence == 0.5


def test_no_fallback_input_can_exceed_the_cap():
    import itertools
    for combo in itertools.product(("LONG", "SHORT", "FLAT", "UNAVAILABLE", "junk"), repeat=4):
        for pos in ("none", "long", "short"):
            v = aggregate(dict(zip(FALLBACK_STRATEGIES, combo)), pos)
            assert 0.0 <= v.confidence <= FALLBACK_MAX_CONFIDENCE < FALLBACK_CONFIDENCE_CEILING


def test_fallback_source_failure_degrades_to_no_trade_not_a_crash():
    class Broken:
        def stances(self, state):
            raise RuntimeError("db locked")
    a = adapter(JevHttpError(), fallback=Broken())
    d = a.evaluate(state())
    assert d.action == "NO_TRADE" and d.confidence == 0.0 and "fallback_failed:RuntimeError" in d.fallback_reason
    assert a.stats["fallback_failed"] == 1


def test_fallback_decision_cannot_become_a_trade_end_to_end():
    d = adapter(JevHttpError()).evaluate(state())          # unanimous LONG, flat book
    assert d.action == "LONG" and d.confidence == 0.89
    res = evaluate_policy(d)
    assert res.policy.tier == "STRATEGY_CHECK" and res.policy.escalate_to == "hermes"
    p = propose_trade(d, res, state(), ExitParams(0.02, 0.04))
    assert p.proposal is None and p.reason == "tier_strategy_check"


def test_fallback_exits_also_stop_at_escalation_known_consequence():
    d = adapter(JevHttpError(), fallback=StaticSource({n: "FLAT" for n in FALLBACK_STRATEGIES})).evaluate(
        state(side="long", size=1.0, entry_price=100000.0))
    assert d.action == "EXIT"
    assert propose_trade(d, evaluate_policy(d), state(side="long", size=1.0, entry_price=100000.0),
                         ExitParams(0.02, 0.04)).proposal is None


# ------------------------------------------------------------ proof of work

def test_logging_proves_the_adapter_ran_and_did_something(caplog):
    with caplog.at_level(logging.INFO, logger="trading.v2.adapter"):
        adapter(raw_ok()).evaluate(state())
        adapter(JevHttpError()).evaluate(state())
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("decision source=jev") and "action=LONG" in m for m in msgs)
    assert any(m.startswith("decision source=fallback") and "http_error" in m for m in msgs)
    assert [r.levelno for r in caplog.records if "fallback" in r.getMessage()] == [logging.WARNING]


def test_mixed_run_stats_add_up():
    flip = {"n": 0}

    def alternate(req, timeout_s):
        flip["n"] += 1
        if flip["n"] % 2 == 0:
            raise JevHttpError()
        return raw_ok()

    a = adapter(alternate)
    for _ in range(10):
        a.evaluate(state())
    assert a.stats["calls"] == 10 and a.stats["jev"] + a.stats["fallback"] == 10
    assert a.stats["jev"] == 5 and a.stats["fallback:http_error"] == 5


def test_adapter_is_deterministic_for_identical_inputs_1000x():
    a = adapter(raw_ok(), timeout_s=5.0)
    s = state()
    first = a.evaluate(s)
    for _ in range(1000):
        d = a.evaluate(s)
        assert d.model_copy(update={"latency_ms": 0}) == first.model_copy(update={"latency_ms": 0})


def test_adapter_caps_fallback_confidence_even_if_the_aggregator_misbehaves(monkeypatch):
    """Defence in depth: the adapter's own cap, independent of fallback.aggregate."""
    import trading.v2.adapter as mod
    from trading.v2.fallback import FallbackVerdict
    monkeypatch.setattr(mod, "aggregate",
                        lambda stances, position: FallbackVerdict("LONG", 0.99, "tsmom", "forged", {}))
    d = adapter(JevHttpError()).evaluate(state())
    assert d.source == "fallback" and d.confidence == FALLBACK_MAX_CONFIDENCE
    monkeypatch.setattr(mod, "aggregate",
                        lambda stances, position: FallbackVerdict("LONG", float("nan"), "tsmom", "forged", {}))
    assert adapter(JevHttpError()).evaluate(state()).confidence == 0.0
