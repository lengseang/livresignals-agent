"""Whole-chain invariants under randomised, partly hostile conditions.

Per-component tests prove each rule in isolation. These prove the rules still hold when everything runs
together with random Jev replies (good, malformed, failing), random context (kill switch, pauses, exchange
health) and random price shocks. Every seed must satisfy EVERY invariant; a failure names its seed.
"""
import json
import random
from collections import Counter

import pytest

from trading.replay.bars import Bar
from trading.shadow.chain import ShadowChain, verify
from trading.v2.adapter import JevAdapter, JevBalanceExhausted, JevHttpError, JevMalformedJson
from trading.v2.contracts import (FALLBACK_STRATEGIES, Decision, ExecutionCommand, PolicyDecision, RiskDecision,
                                  TradeProposal)
from trading.v2.fallback import GatedSource, StaticSource
from trading.v2.killswitch import KillController, KillLimits
from trading.v2.paper import PaperBook
from trading.v2.risk import ExchangeHealth, RiskLimits
from trading.v2.shadow import ShadowPipeline
from trading.v2.workflow import load_active
from trading.v2.context import PaperContextProvider
from trading.v2.contracts import StrategyRef
from trading.v2.policy import ExitParams

from test_v2_adapter import raw_ok
from test_v2_killswitch import obs, req
from test_v2_paper import FEE, SYM
from test_v2_shadow import FollowBook, make_bars
from test_v2_features import T0

WF = load_active()
ACTIONS = ("LONG", "SHORT", "HOLD", "EXIT", "REDUCE", "NO_TRADE")
COVER: Counter = Counter()
OUTCOMES = {"no_snapshot", "snapshot_unusable", "ignored", "watch", "escalate", "no_proposal", "risk_rejected",
            "would_execute", "cycle_error"}
LIM = KillLimits(3, 60_000, 5, 50.0, 0.2, None, 0.8)


def random_reply(rng):
    kind = rng.random()
    if kind < 0.10:
        return JevHttpError("x")
    if kind < 0.15:
        return JevMalformedJson("x")
    if kind < 0.18:
        return JevBalanceExhausted("x")
    if kind < 0.25:
        return rng.choice(["nonsense", None, [], {}, {"model": "m"}])
    reply = raw_ok()
    a = rng.choices(ACTIONS, weights=[35, 10, 15, 15, 10, 15])[0]        # LONG-heavy so trades actually happen
    conf = rng.choice([0.2, 0.6, 0.8, 0.92, 0.95, 0.99, 0.99])
    rest = [rng.random() for _ in range(5)]
    scale = (1 - conf) / sum(rest)
    dist = dict(zip([x for x in ACTIONS if x != a], [r * scale for r in rest]))
    dist[a] = conf
    reply["answers"]["action"] = {"value": a, "probability": conf, "distribution": dist}
    reply["answers"]["entry_quality"]["value"] = rng.choice([0.3, 0.69, 0.7, 0.9, 0.9, 0.9])
    reply["answers"]["risk_level"]["value"] = rng.choice(["LOW", "MEDIUM", "MEDIUM", "HIGH", "EXTREME"])
    if rng.random() < 0.10:                                   # hostile near-miss
        rng.choice([lambda: reply["answers"]["trend_quality"].update(value="high"),
                    lambda: reply["answers"]["action"].update(value="BUY"),
                    lambda: reply["answers"]["action"]["distribution"].update(LONG=0.0)])()
    return reply


class RandomClient:
    def __init__(self, rng):
        self.rng = rng

    def evaluate(self, request, timeout_s):
        r = random_reply(self.rng)
        if isinstance(r, Exception):
            raise r
        return json.loads(json.dumps(r))


def shocked_bars(rng):
    bars = make_bars(n1=700)
    shock_at = T0 + rng.randrange(260, 640) * 60_000
    f = rng.choice([1.0, 0.93, 0.93, 1.06])
    out = []
    for x in bars:
        if x.interval == "1m" and x.open_ms >= shock_at and f != 1.0:
            out.append(Bar(x.symbol, "1m", x.open_ms, x.end_ms, x.open * f, x.high * f, x.low * f, x.close * f,
                           x.volume, x.quote_volume * f, x.trades, x.taker_buy_base))
        else:
            out.append(x)
    return out


def build(tmp, seed):
    rng = random.Random(seed)
    book = PaperBook(10_000.0, FEE, ShadowChain(tmp, "v2paper", "book", T0))
    ctl = KillController(LIM)
    if rng.random() < 0.12:
        ctl.observe(obs(orders_last_minute=99))
    if rng.random() < 0.10:
        ctl.request_halt(req(action="HALT_SYMBOL", target=SYM), 1)
    health = rng.choice([ExchangeHealth(True, "ok")] * 6 + [ExchangeHealth(False, "503"), None])
    provider = PaperContextProvider(
        book, StrategyRef(name="btc_breakout_v3", version="3.2.1"),
        RiskLimits(daily_loss_cap_usd=rng.choice([100.0, 1_000.0, None])), exit_params=ExitParams(0.02, 0.04),
        daily_loss_budget_usd=50.0, exchange_health=lambda: health, kill_switch=lambda: ctl.engaged,
        paused=lambda: ctl.paused_scopes,
        legacy_action=lambda s, t: rng.choice([None, "LONG", "NO_TRADE", "EXIT"]))
    stances = {n: rng.choice(["LONG", "FLAT", "SHORT", "UNAVAILABLE"]) for n in FALLBACK_STRATEGIES}
    adapter = JevAdapter(RandomClient(rng), WF,
                         GatedSource(StaticSource(stances), enabled=rng.choice([
                             ("trend_follower", "donchian_ensemble"), FALLBACK_STRATEGIES])),
                         timeout_s=1.0, clock=lambda: 0.0)
    fb = FollowBook()
    p = ShadowPipeline(adapter=adapter, provider=provider, sink=ShadowChain(tmp, "v2cycle", "pipeline", T0),
                       book=fb, limits=provider.limits, cadence_minutes=rng.choice([5, 15]), paper=book)
    fb.pipeline = p
    return p, book, ctl, rng


@pytest.mark.parametrize("seed", range(60))
def test_every_cycle_of_a_randomised_run_satisfies_every_safety_invariant(tmp_path, seed):
    p, book, ctl, rng = build(tmp_path, seed)
    recs = p.run(shocked_bars(rng))
    ctx = f"seed={seed}"
    COVER["seeds"] += 1
    for r in recs:
        COVER["outcome:" + r["outcome"]] += 1
        if r.get("risk") and not r["risk"]["approved"]:
            COVER["risk_reject:" + r["risk"]["reject_reason"]] += 1
        if r.get("decision"):
            COVER["source:" + r["decision"]["source"]] += 1
            if r["decision"]["action"] in ("EXIT", "REDUCE") and r["proposal"] is not None:
                COVER["exit_proposed"] += 1
    for k in ("opened", "closed", "stopped"):
        COVER[k] += book.stats[k]
    COVER["kill_engaged_seeds"] += bool(ctl.engaged)
    COVER["paused_seeds"] += bool(ctl.paused)
    assert recs, ctx
    # bookkeeping: proves the run did work and nothing was lost
    assert p.stats["cycles"] == p.stats["records"] == len(recs), ctx
    assert sum(p.stats[f"outcome:{o}"] for o in OUTCOMES) == len(recs), ctx
    assert {r["outcome"] for r in recs} <= OUTCOMES, ctx
    assert p.stats["cycle_error"] == 0, (ctx, [r for r in recs if r["outcome"] == "cycle_error"])
    assert verify(tmp_path / "v2cycle" / "pipeline")["ok"] and verify(tmp_path / "v2paper" / "book")["ok"], ctx

    would = 0
    for r in recs:
        assert r["real"] is False and r["mode"] == "paper" and "not real money" in r["label"], ctx
        if r["outcome"] in ("no_snapshot", "snapshot_unusable"):
            assert r.get("decision") is None and r.get("execution_preview") is None, ctx
            continue
        dec = Decision.from_dict(r["decision"])
        pol = PolicyDecision.from_dict(r["policy"])
        # fallback can never reach EXECUTE, a proposal, risk, or a preview
        if dec.source == "fallback":
            assert dec.confidence <= 0.89 and dec.fallback_reason and dec.fallback_strategy, ctx
            assert pol.tier != "EXECUTE" and r["proposal"] is None and r["risk"] is None, ctx
            assert r["execution_preview"] is None and r["outcome"] != "would_execute", ctx
        # policy only demotes
        assert pol.adjusted_confidence <= dec.confidence + 1e-12, ctx
        if dec.action in ("HOLD", "NO_TRADE"):
            assert pol.tier == "IGNORE", ctx
        if r["proposal"] is not None:
            prop = TradeProposal.from_dict(r["proposal"])
            assert "size" not in r["proposal"] and pol.tier == "EXECUTE" and dec.source == "jev", ctx
            assert prop.rationale.decision_id == dec.decision_id, ctx
        # a preview exists iff risk approved, and carries the risk decision's size/stop, never a proposal's
        if r["execution_preview"] is not None:
            risk = RiskDecision.from_dict(r["risk"])
            cmd = ExecutionCommand.from_dict(r["execution_preview"])
            assert risk.approved and r["outcome"] == "would_execute" and dec.source == "jev", ctx
            assert cmd.size == risk.size and cmd.risk_decision_id == risk.risk_decision_id, ctx
            assert cmd.stop_price == risk.stop_price and (cmd.stop_price is not None) != cmd.reduce_only, ctx
            would += 1
        else:
            assert r["outcome"] != "would_execute", ctx
        if r["risk"] is not None:
            risk = RiskDecision.from_dict(r["risk"])
            if risk.approved:
                # approved implies none of the blocking facts were present at that cycle
                names = {c.name: c.passed for c in risk.checks}
                assert all(names[n] for n in ("kill_switch", "exchange_health", "daily_loss_limit",
                                              "duplicate_order", "halt_scope", "buying_power") if n in names), ctx
        if r["state"]["position"]["side"] == "none" and r["outcome"] == "would_execute":
            assert r["proposal"]["intent"] == "entry", ctx

    # paper book: exactly the would-executes were submitted, never two positions, money conserved
    assert book.stats["queued"] == would, (ctx, dict(book.stats), would)
    assert len(book.positions) <= 1, ctx
    assert book.stats["opened"] + book.stats["rejected:position_exists"] + book.stats["rejected:insufficient_cash"] \
        + len(book.pending) + book.stats.get("duplicate_ignored", 0) >= 0, ctx
    if not book.positions:
        assert book.cash == pytest.approx(10_000.0 + sum(t["pnl"] for t in book.trades), abs=1e-6), ctx
    assert book.stats["rejected:insufficient_cash"] == 0, (ctx, "risk approved an order cash could not fund")
    COVER["slippage_refused"] += book.stats["rejected:slippage_exceeded"]
    # a latched kill switch or a pause on the symbol means no entry was ever queued
    if ctl.engaged or ("symbol", SYM) in ctl.paused or ("all", "*") in ctl.paused:
        assert book.stats["queued"] == 0, ctx


def test_the_random_runs_actually_exercised_the_dangerous_paths():
    """Guards against a vacuous pass: the invariants above only mean something if the runs traded, stopped
    out, exited, were vetoed by risk, hit a latched kill switch and fell back."""
    c = COVER
    assert c["seeds"] == 60, "run the whole file (this test reads what the seeds recorded)"
    floors = {"outcome:would_execute": 40, "opened": 25, "closed": 10, "stopped": 3, "exit_proposed": 3,
              "source:fallback": 100, "source:jev": 400, "risk_reject:kill_switch": 5,
              "risk_reject:exchange_health": 3, "outcome:escalate": 20, "outcome:ignored": 100,
              "kill_engaged_seeds": 3, "paused_seeds": 3, "slippage_refused": 1,
              "risk_reject:daily_loss_limit": 5, "risk_reject:halt_scope": 3, "risk_reject:strategy_breaker": 2,
              "risk_reject:max_position_size": 5}
    short = {k: (c[k], need) for k, need in floors.items() if c[k] < need}
    assert not short, f"under-exercised (got, needed): {short}; coverage={dict(c)}"


# ================================================================== V2-15: position management under the same chaos

from trading.v2.advisor import PositionAdvisor                                   # noqa: E402
from trading.v2.contracts import PositionAdjustment                              # noqa: E402
from trading.v2.gateway import Gateway                                           # noqa: E402
from trading.v2.position import RatchetRule                                      # noqa: E402
from trading.v2.workflow import load_position_workflow                           # noqa: E402

PWF = load_position_workflow()
PACTS = ("HOLD", "TIGHTEN_STOP", "REDUCE", "EXIT")
COVER2: Counter = Counter()
OUTCOMES2 = OUTCOMES | {"position_hold", "position_tighten", "position_tighten_refused", "position_exit",
                        "position_reduce", "position_risk_rejected"}


class RandomPositionClient:
    def __init__(self, rng):
        self.rng = rng

    def evaluate(self, request, timeout_s):
        r = self.rng.random()
        if r < 0.15:
            raise JevHttpError("x")
        if r < 0.20:
            return self.rng.choice(["junk", {}, {"model": "m", "answers": {}}])
        a = self.rng.choices(PACTS, weights=[40, 25, 15, 20])[0]
        conf = self.rng.choice([0.3, 0.55, 0.65, 0.75, 0.9])
        rest = [self.rng.random() for _ in range(3)]
        dist = dict(zip([x for x in PACTS if x != a], [x * (1 - conf) / sum(rest) for x in rest]))
        dist[a] = conf
        if self.rng.random() < 0.05:
            a = "LONG"                                    # hostile: an entry smuggled into position advice
        return {"model": "jev-1.13.0", "answers": {
            "position_action": {"value": a, "probability": None, "distribution": dist},
            **{k: {"value": self.rng.random()} for k in ("momentum_fading", "reversal_risk", "target_reachable")}}}


def build2(tmp, seed):
    p, book, ctl, rng = build(tmp, 1000 + seed)
    book.ratchet = RatchetRule(activate_frac=rng.choice([0.25, 0.5]), lock_frac=0.5)
    p.provider.exit_params = ExitParams(0.01, rng.choice([0.015, 0.02]))
    gw = Gateway()
    p.adapter.gateway = gw
    p.advisor = PositionAdvisor(RandomPositionClient(rng), PWF, gw, timeout_s=1.0, clock=lambda: 0.0)
    return p, book, ctl, rng


@pytest.mark.parametrize("seed", range(60))
def test_position_management_invariants_under_random_advice_and_shocks(tmp_path, seed):
    p, book, ctl, rng = build2(tmp_path, seed)
    recs = p.run(shocked_bars(rng))
    ctx = f"seed={seed}"
    COVER2["seeds"] += 1
    assert p.stats["cycle_error"] == 0, (ctx, [r.get("error") for r in recs if r["outcome"] == "cycle_error"])
    assert {r["outcome"] for r in recs} <= OUTCOMES2, ctx
    assert verify(tmp_path / "v2paper" / "book")["ok"], ctx
    for r in recs:
        COVER2["outcome:" + r["outcome"]] += 1
        if r.get("advice"):
            COVER2["advice:" + r["advice"]["source"]] += 1
            assert r["advice"]["action"] in PACTS, ctx                      # never an entry, whatever Jev said
            if r["execution_preview"] is not None:
                assert r["execution_preview"]["reduce_only"] is True, ctx  # advice can only reduce
            if r["outcome"] in ("position_exit", "position_reduce"):
                assert RiskDecision.from_dict(r["risk"]).approved, ctx
        if r.get("decision") and r["decision"]["source"] == "fallback":
            COVER2["fallback:" + r["decision"]["fallback_strategy"]] += 1
            assert r["outcome"] != "would_execute", ctx                     # the gateway never opens a trade
        if r.get("gateway_dual_run"):
            COVER2["dual_run"] += 1
    # every stop amendment tightened, per position the stops only moved one way
    last: dict = {}
    for a in book.adjustments:
        PositionAdjustment.from_dict(a.to_dict())
        COVER2["tightened:" + a.source] += 1
        k = (a.symbol, a.position_side)
        if k in last and a.old_stop == last[k]:
            assert (a.new_stop > a.old_stop) if a.position_side == "LONG" else (a.new_stop < a.old_stop), ctx
        last[k] = a.new_stop
    for t in book.trades:
        COVER2["exit:" + t["reason"]] += 1
        assert t["mfe"] >= 0 >= t["mae"], ctx
    if not book.positions:
        assert book.cash == pytest.approx(10_000.0 + sum(t["pnl"] for t in book.trades), abs=1e-6), ctx
    assert len(book.positions) <= 1 and book.stats["rejected:insufficient_cash"] == 0, ctx


def test_the_position_runs_exercised_every_new_path():
    c = COVER2
    assert c["seeds"] == 60, "run the whole file"
    floors = {"advice:jev": 30, "advice:gateway": 10, "tightened:ratchet": 5, "tightened:jev": 5,
              "exit:target": 3, "exit:exit": 3, "exit:stop": 3, "exit:reduce": 3, "outcome:position_exit": 3,
              "outcome:position_hold": 30, "outcome:position_tighten": 5, "fallback:system_one_gateway": 50,
              "dual_run": 100}
    short = {k: (c[k], need) for k, need in floors.items() if c[k] < need}
    assert not short, f"under-exercised (got, needed): {short}; coverage={dict(c)}"
