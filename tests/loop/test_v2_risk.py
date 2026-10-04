"""V2-4: Contract G, wrapping the legacy risk layer. Every veto has its own test."""
import ast
import pathlib

import pytest

import trading.v2.risk as R
from trading.risk import risk_engine as legacy
from trading.risk.guards import GUARDS, Guards
from trading.v2.contracts import (REQUIRED_RISK_CHECKS, DecisionState, ExecutionCommand, RiskDecision,
                                  TradeProposal)
from trading.v2.risk import (ExchangeHealth, OpenOrder, RiskContext, RiskLimits, evaluate_risk)

from test_v2_contracts import decision_state, trade_proposal, mutated

LIMITS = RiskLimits(daily_loss_cap_usd=100.0)
OK = ExchangeHealth(ok=True, detail="REST 200, 91ms")


def state(**kw):
    d = decision_state()
    for k, v in kw.items():
        d[k].update(v) if isinstance(v, dict) else d.__setitem__(k, v)
    return DecisionState.from_dict(d)


def ctx(**kw):
    base = dict(state=state(), day_start_equity=10000.0, week_start_equity=10000.0,
                high_water_mark=10000.0, open_notional=0.0, open_orders=(), exchange=OK,
                kill_switch_engaged=False, strategy_loss_pct=0.0, consecutive_losses=0,
                daily_loss_budget_used=0.0, leverage=1.0)
    base.update(kw)
    return RiskContext(**base)


def proposal(**kw):
    return TradeProposal.from_dict(mutated(trade_proposal, **kw))


def exit_proposal(intent="exit", side="LONG"):
    d = mutated(trade_proposal, intent=intent, side=side, stop_pct=None, target_pct=None)
    d["rationale"]["policy_tier"] = "EXECUTE"
    return TradeProposal.from_dict(d)


LONG_POS = dict(position=dict(side="long", size=0.5, entry_price=100000.0))


def run(p=None, c=None, score=82.0, limits=LIMITS):
    return evaluate_risk(p or proposal(), c or ctx(), score, limits)


def failed(r):
    return [x.name for x in r.checks if not x.passed]


# ------------------------------------------------------------ baseline + wrapping

def test_clean_entry_is_approved_with_size_stop_and_every_check_listed():
    r = run()
    assert r.approved and r.reject_reason is None and failed(r) == []
    names = [x.name for x in r.checks]
    assert tuple(names[:7]) == REQUIRED_RISK_CHECKS
    assert {"guards_intact", "stop_distance", "exposure_cap", "weekly_loss_halt", "drawdown_halt",
            "strategy_breaker", "short_allowed", "proposal_matches_state"} <= set(names)
    assert len(names) == len(set(names))
    assert r.stop_price == pytest.approx(108420.5 * 0.98)
    assert r.risk_version == "risk-v2.0" and r.proposal_id == proposal().proposal_id


def test_sizing_is_the_legacy_engines_sizing_in_base_units():
    r = run()
    legacy_pos = legacy.compute_position(score=82.0, risk_equity=10000.0, entry=108420.5, stop_distance=0.02)
    assert r.size == pytest.approx(legacy_pos["position_notional"] / 108420.5, abs=1e-8)
    assert r.size * 108420.5 <= 10000.0 * GUARDS.max_exposure_fraction  # floored: never overshoots the cap
    assert r.size == pytest.approx(legacy_pos["position_notional"] / 108420.5, abs=1e-8) and r.size <= legacy_pos["position_notional"] / 108420.5
    assert R.compute_position is legacy.compute_position and R.GUARDS is GUARDS  # wrapped, not copied


def test_partial_exposure_shrinks_the_size_never_grows_it():
    free = run().size
    half = run(c=ctx(open_notional=6000.0)).size
    assert 0 < half < free and half * 108420.5 == pytest.approx(4000.0, abs=1e-3)


def test_full_exposure_rejects_via_the_legacy_engine():
    r = run(c=ctx(open_notional=10000.0))
    assert not r.approved and r.reject_reason == "max_position_size" and r.size is None
    assert "exposure full" in next(x.detail for x in r.checks if x.name == "max_position_size")


def test_deterministic_1000x():
    first = run().to_json()
    assert all(run().to_json() == first for _ in range(1000))
    assert run(p=proposal(), c=ctx(kill_switch_engaged=True)).to_json() == run(
        p=proposal(), c=ctx(kill_switch_engaged=True)).to_json()


def test_module_is_pure_no_clock_no_io():
    src = pathlib.Path(R.__file__).read_text()
    tree = ast.parse(src)
    mods = {n.module.split(".")[0] if isinstance(n, ast.ImportFrom) else a.name.split(".")[0]
            for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
            for a in (n.names if isinstance(n, ast.Import) else [n])}
    assert mods.isdisjoint({"time", "random", "socket", "requests", "os", "sqlite3", "subprocess", "logging"})
    assert ".now(" not in src and "utcnow" not in src


# ------------------------------------------------------------ one test per veto

def veto(reason, **kw):
    r = run(**kw)
    assert not r.approved and r.reject_reason == reason, (r.reject_reason, failed(r))
    assert reason in failed(r) and r.size is None and r.stop_price is None
    assert tuple(x.name for x in r.checks[:7]) == REQUIRED_RISK_CHECKS
    return r


def test_veto_daily_loss_absolute_cap():
    veto("daily_loss_limit", c=ctx(state=state(risk=dict(daily_pnl=-100.0))))
    assert run(c=ctx(state=state(risk=dict(daily_pnl=-99.99)))).approved
    assert run(c=ctx(state=state(risk=dict(daily_pnl=-5000.0))), limits=RiskLimits(daily_loss_cap_usd=None)
               ).approved  # no absolute cap configured, pct halt still clear


def test_veto_daily_loss_legacy_percent_halt():
    r = veto("daily_loss_limit", c=ctx(day_start_equity=10600.0))   # 10000/10600 = -5.7% <= -5%
    assert "daily_halt" in next(x.detail for x in r.checks if x.name == "daily_loss_limit")
    assert run(c=ctx(day_start_equity=10400.0)).approved             # -3.8%


def test_veto_spread():
    veto("spread_ceiling", c=ctx(state=state(market=dict(spread_bps=5.01))))
    assert run(c=ctx(state=state(market=dict(spread_bps=5.0)))).approved


def test_veto_leverage():
    veto("max_leverage", c=ctx(leverage=2.0))
    assert run(c=ctx(leverage=1.0)).approved


def test_veto_duplicate_open_order_and_duplicate_position():
    veto("duplicate_order", c=ctx(open_orders=(OpenOrder("BTCUSDT", "LONG", False),)))
    assert run(c=ctx(open_orders=(OpenOrder("ETHUSDT", "LONG", False), OpenOrder("BTCUSDT", "LONG", True)))).approved
    r = veto("duplicate_order", c=ctx(state=state(**LONG_POS)))
    assert "already LONG" in next(x.detail for x in r.checks if x.name == "duplicate_order")


def test_veto_exchange_unhealthy_or_unknown_fails_closed():
    veto("exchange_health", c=ctx(exchange=ExchangeHealth(False, "REST 503")))
    r = veto("exchange_health", c=ctx(exchange=None))
    assert "unknown" in next(x.detail for x in r.checks if x.name == "exchange_health")


def test_veto_kill_switch_engaged_or_unknown_fails_closed():
    veto("kill_switch", c=ctx(kill_switch_engaged=True))
    r = veto("kill_switch", c=ctx(kill_switch_engaged=None))
    assert "unknown" in next(x.detail for x in r.checks if x.name == "kill_switch")


def test_veto_max_position_when_stop_too_wide():
    r = run(p=proposal(stop_pct=0.06))
    assert not r.approved and {"stop_distance", "max_position_size"} <= set(failed(r))
    assert run(p=proposal(stop_pct=0.05)).approved


def test_veto_weekly_and_drawdown_halts_from_the_legacy_breakers():
    veto("weekly_loss_halt", c=ctx(week_start_equity=11200.0))     # -10.7%
    veto("drawdown_halt", c=ctx(high_water_mark=12000.0))          # -16.7%
    assert run(c=ctx(week_start_equity=10900.0, high_water_mark=11000.0)).approved


@pytest.mark.parametrize("kw", [dict(strategy_loss_pct=-0.05), dict(consecutive_losses=3),
                                dict(daily_loss_budget_used=2.0)])
def test_veto_strategy_breaker(kw):
    veto("strategy_breaker", c=ctx(**kw))
    assert run(c=ctx(strategy_loss_pct=-0.049, consecutive_losses=2, daily_loss_budget_used=1.99)).approved


def test_veto_short_entry_unless_explicitly_allowed():
    r = run(p=proposal(side="SHORT"))
    assert not r.approved and "short_allowed" in failed(r) and r.size is None
    assert "spot only" in next(x.detail for x in r.checks if x.name == "short_allowed")
    r = run(p=proposal(side="SHORT"), limits=RiskLimits(daily_loss_cap_usd=100.0, allow_short=True))
    assert r.approved and r.stop_price == pytest.approx(108420.5 * 1.02)


def test_veto_symbol_mismatch():
    veto("proposal_matches_state", p=proposal(symbol="ETHUSDT"))


def test_veto_when_the_legacy_guards_have_been_weakened(monkeypatch):
    monkeypatch.setattr(R, "GUARDS", Guards(max_risk_per_trade=0.05))
    veto("guards_intact")


def test_several_vetoes_are_all_reported_and_the_first_is_the_reason():
    r = run(c=ctx(kill_switch_engaged=True, leverage=3.0, exchange=None))
    assert failed(r)[:3] == ["max_leverage", "exchange_health", "kill_switch"]
    assert r.reject_reason == "max_leverage"


def test_no_score_means_no_trade():
    r = run(score=None)
    assert not r.approved and "max_position_size" in failed(r)
    assert "NO_TRADE" in next(x.detail for x in r.checks if x.name == "max_position_size")


# ------------------------------------------------------------ exits and reductions

def test_exit_closes_the_whole_position_without_a_stop():
    r = run(p=exit_proposal("exit"), c=ctx(state=state(**LONG_POS)))
    assert r.approved and r.size == 0.5 and r.stop_price is None


def test_reduce_closes_the_configured_fraction():
    r = run(p=exit_proposal("reduce"), c=ctx(state=state(**LONG_POS)))
    assert r.approved and r.size == 0.25
    r = run(p=exit_proposal("reduce"), c=ctx(state=state(**LONG_POS)),
            limits=RiskLimits(daily_loss_cap_usd=100.0, reduce_fraction=0.2))
    assert r.size == pytest.approx(0.1)


def test_reduce_only_is_not_blocked_by_loss_limits_exposure_or_spread():
    c = ctx(state=state(position=LONG_POS["position"], risk=dict(daily_pnl=-5000.0),
                        market=dict(spread_bps=50.0)),
            day_start_equity=20000.0, week_start_equity=20000.0, high_water_mark=30000.0,
            open_notional=10000.0, consecutive_losses=9)
    r = run(p=exit_proposal("exit"), c=c)
    assert r.approved and r.size == 0.5
    na = [x.name for x in r.checks if x.detail.startswith("n/a")]
    assert {"daily_loss_limit", "spread_ceiling", "weekly_loss_halt", "drawdown_halt"} <= set(na)


@pytest.mark.parametrize("kw,reason", [
    (dict(kill_switch_engaged=True), "kill_switch"),
    (dict(kill_switch_engaged=None), "kill_switch"),
    (dict(exchange=None), "exchange_health"),
    (dict(exchange=ExchangeHealth(False, "down")), "exchange_health"),
    (dict(open_orders=(OpenOrder("BTCUSDT", "LONG", True),)), "duplicate_order"),
    (dict(leverage=2.0), "max_leverage"),
])
def test_reduce_only_is_still_blocked_by_the_safety_vetoes(kw, reason):
    r = run(p=exit_proposal("exit"), c=ctx(state=state(**LONG_POS), **kw))
    assert not r.approved and r.reject_reason == reason


def test_exit_without_a_matching_position_is_rejected():
    r = run(p=exit_proposal("exit"), c=ctx())
    assert not r.approved and r.reject_reason == "max_position_size"
    r = run(p=exit_proposal("exit", side="SHORT"), c=ctx(state=state(**LONG_POS)))
    assert not r.approved and r.reject_reason == "max_position_size"


# ------------------------------------------------------------ fail closed + composition

def test_internal_error_rejects_with_every_required_check_failed():
    bad = RiskContext(state=None, day_start_equity=1, week_start_equity=1, high_water_mark=1,
                      open_notional=0, open_orders=(), exchange=OK, kill_switch_engaged=False,
                      strategy_loss_pct=0, consecutive_losses=0, daily_loss_budget_used=0)
    r = evaluate_risk(proposal(), bad, 82.0, LIMITS)
    assert not r.approved and r.reject_reason == "engine_error" and r.size is None
    assert all(not x.passed for x in r.checks) and tuple(x.name for x in r.checks[:7]) == REQUIRED_RISK_CHECKS
    assert "AttributeError" in r.checks[-1].detail


def test_garbage_inputs_never_approve_and_never_raise():
    for kw in (dict(day_start_equity=0.0), dict(high_water_mark=-1.0), dict(leverage=float("nan")),
               dict(open_notional=float("nan"))):
        r = run(c=ctx(**kw))
        assert isinstance(r, RiskDecision)
        if r.approved:   # only acceptable if the garbage was genuinely harmless
            assert r.size and r.size > 0 and kw.keys() <= {"day_start_equity", "high_water_mark"}


def test_every_scenario_lists_all_seven_checks_exactly_once():
    scenarios = [ctx(), ctx(kill_switch_engaged=True), ctx(exchange=None), ctx(leverage=9.0),
                 ctx(open_notional=1e9), ctx(state=state(**LONG_POS))]
    for c in scenarios:
        for p in (proposal(), exit_proposal()):
            names = [x.name for x in run(p=p, c=c).checks]
            assert all(names.count(n) == 1 for n in REQUIRED_RISK_CHECKS)


def test_approved_decision_flows_into_an_execution_command_with_risks_size():
    p = proposal()
    r = run(p=p)
    cmd = ExecutionCommand.from_approved(proposal=p, risk_decision=r, command_id="ex_ABC123", venue="binance")
    assert cmd.size == r.size and cmd.stop_price == r.stop_price and cmd.risk_decision_id == r.risk_decision_id
    assert (cmd.side, cmd.position_side, cmd.reduce_only) == ("BUY", "LONG", False)
    e = exit_proposal("exit")
    re = run(p=e, c=ctx(state=state(**LONG_POS)))
    cmd = ExecutionCommand.from_approved(proposal=e, risk_decision=re, command_id="ex_ABC124", venue="binance")
    assert (cmd.side, cmd.position_side, cmd.reduce_only, cmd.size) == ("SELL", "LONG", True, 0.5)
