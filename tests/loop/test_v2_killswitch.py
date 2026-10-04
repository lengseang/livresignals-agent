"""V2-10: the deterministic kill controller, Hermes halt requests, and their effect on risk and paper."""
import ast
import json
import pathlib

import pytest

from trading.shadow.chain import ShadowChain, verify
from trading.v2.contracts import HaltRequest
from trading.v2.killswitch import HaltDecision, KillController, KillLimits, Observation
from trading.v2.paper import PaperBook
from trading.v2.risk import ExchangeHealth, RiskLimits, evaluate_risk

from test_v2_contracts import halt_request, mutated
from test_v2_risk import LIMITS, LONG_POS, ctx, exit_proposal, proposal, run, state, failed
from test_v2_paper import T0, FEE, cmd, paper_pipeline, make_bars, M
from test_v2_paper import SYM

LIM = KillLimits(exchange_failures_to_engage=3, max_feed_age_ms=60_000, max_orders_per_minute=5,
                 max_abs_slippage_bps=50.0, max_price_move_frac=0.2, daily_loss_cap_usd=100.0,
                 min_request_confidence=0.8)
OK = ExchangeHealth(True, "REST 200")


def obs(**kw):
    base = dict(now_ms=1_000_000, exchange=OK, feed_age_ms=1_000, orders_last_minute=0, unexpected_positions=(),
                recent_slippage_bps=None, price=100.0, prev_price=100.0, daily_pnl=-10.0)
    base.update(kw)
    return Observation(**base)


def req(**kw):
    return HaltRequest.from_dict(mutated(halt_request, **kw))


# ------------------------------------------------------------ triggers, each in isolation

def test_clean_observations_never_engage():
    c = KillController(LIM)
    for i in range(50):
        s = c.observe(obs(now_ms=i))
    assert not s.engaged and s.reasons == () and not s.newly_engaged and c.log == []


@pytest.mark.parametrize("kw,reason", [
    (dict(feed_age_ms=60_001), "feed_stale"),
    (dict(orders_last_minute=6), "runaway_order_rate"),
    (dict(unexpected_positions=("SOLUSDT", "ADAUSDT")), "unexpected_position:ADAUSDT,SOLUSDT"),
    (dict(recent_slippage_bps=50.01), "abnormal_slippage"),
    (dict(recent_slippage_bps=-50.01), "abnormal_slippage"),
    (dict(price=121.0), "price_jump"),
    (dict(price=79.0), "price_jump"),
    (dict(price=0.0), "price_invalid"),
    (dict(price=-5.0), "price_invalid"),
    (dict(price=float("nan")), "price_invalid"),
    (dict(daily_pnl=-100.0), "daily_loss_exceeded"),
])
def test_each_emergency_condition_engages_with_its_reason(kw, reason):
    s = KillController(LIM).observe(obs(**kw))
    assert s.engaged and s.newly_engaged and s.reasons == (reason,) and s.engaged_at_ms == 1_000_000


@pytest.mark.parametrize("kw", [dict(feed_age_ms=60_000), dict(orders_last_minute=5), dict(recent_slippage_bps=50.0),
                                dict(recent_slippage_bps=-50.0), dict(price=120.0), dict(price=80.0),
                                dict(daily_pnl=-99.99), dict(recent_slippage_bps=None), dict(prev_price=None)])
def test_boundaries_are_inclusive_of_the_limit_and_missing_slippage_or_prev_price_is_not_a_trigger(kw):
    assert not KillController(LIM).observe(obs(**kw)).engaged


@pytest.mark.parametrize("kw,reason", [
    (dict(feed_age_ms=None), "unknown_feed_age"), (dict(orders_last_minute=None), "unknown_order_rate"),
    (dict(unexpected_positions=None), "unknown_positions"), (dict(daily_pnl=None), "unknown_daily_pnl"),
    (dict(price=None), "price_invalid"), (dict(feed_age_ms=float("inf")), "unknown_feed_age"),
])
def test_unknown_required_observations_fail_closed(kw, reason):
    s = KillController(LIM).observe(obs(**kw))
    assert s.engaged and reason in s.reasons


def test_exchange_must_fail_n_times_in_a_row_and_one_good_reading_resets_the_streak():
    c = KillController(LIM)
    bad = ExchangeHealth(False, "REST 503")
    assert not c.observe(obs(exchange=bad)).engaged
    assert not c.observe(obs(exchange=bad)).engaged
    assert not c.observe(obs(exchange=OK)).engaged                    # streak reset
    assert not c.observe(obs(exchange=bad)).engaged
    assert not c.observe(obs(exchange=bad)).engaged
    s = c.observe(obs(exchange=bad))
    assert s.engaged and s.reasons == ("exchange_down",)


def test_unknown_exchange_health_counts_as_a_failure_too():
    c = KillController(LIM)
    for _ in range(2):
        assert not c.observe(obs(exchange=None)).engaged
    assert c.observe(obs(exchange=None)).reasons == ("unknown_exchange",)


def test_several_triggers_are_all_reported():
    s = KillController(LIM).observe(obs(feed_age_ms=99_999, orders_last_minute=50, daily_pnl=-500.0))
    assert set(s.reasons) == {"feed_stale", "runaway_order_rate", "daily_loss_exceeded"}


# ------------------------------------------------------------ latch and human reset

def test_once_engaged_it_stays_engaged_when_conditions_clear():
    c = KillController(LIM)
    first = c.observe(obs(now_ms=10, orders_last_minute=9))
    assert first.engaged and first.newly_engaged
    later = c.observe(obs(now_ms=20))                                   # everything clean again
    assert later.engaged and not later.newly_engaged and later.engaged_at_ms == 10
    assert later.reasons == ("runaway_order_rate",)
    more = c.observe(obs(now_ms=30, daily_pnl=-200.0))
    assert more.reasons == ("runaway_order_rate", "daily_loss_exceeded") and more.engaged_at_ms == 10
    assert [e["event"] for e in c.log] == ["engaged"]                   # one transition, not one per tick


def test_only_a_named_human_can_reset_and_a_reset_can_engage_again():
    c = KillController(LIM)
    c.observe(obs(now_ms=10, orders_last_minute=9))
    for who in ("", "   "):
        with pytest.raises(ValueError, match="human"):
            c.reset(who, 11)
    assert c.engaged
    c.reset("owner", 12)
    assert not c.engaged and c.reasons == [] and c.engaged_at_ms is None
    assert c.log[-1] == {"mode": "PAPER_V2", "type": "kill_event", "real": False, "event": "reset",
                         "ts_event_ms": 12, "by": "owner", "cleared": ["runaway_order_rate"]}
    s = c.observe(obs(now_ms=13, orders_last_minute=9))
    assert s.engaged and s.newly_engaged and s.engaged_at_ms == 13


def test_limits_are_validated():
    with pytest.raises(ValueError):
        KillController(KillLimits(0, 1, 1, 1.0, 0.1, None, 0.5))


# ------------------------------------------------------------ Hermes halt requests

def test_halt_requests_only_pause_new_entries_and_are_logged():
    c = KillController(LIM)
    d = c.request_halt(req(action="HALT_STRATEGY", target="btc_breakout_v3"), 5)
    assert d == HaltDecision(True, "new entries paused for strategy=btc_breakout_v3")
    c.request_halt(req(action="HALT_SYMBOL", target="ETHUSDT"), 6)
    c.request_halt(req(action="HALT_ALL", target=None), 7)
    assert c.paused_scopes == (("all", "*"), ("strategy", "btc_breakout_v3"), ("symbol", "ETHUSDT"))
    assert not c.engaged                                                # a halt request is NOT the kill switch
    assert [e["event"] for e in c.log] == ["halt_accepted"] * 3 and all(e["real"] is False for e in c.log)
    c.request_halt(req(action="HALT_SYMBOL", target="ETHUSDT"), 8)      # idempotent
    assert len(c.paused) == 3


def test_low_confidence_requests_are_rejected_and_logged():
    c = KillController(LIM)
    d = c.request_halt(req(confidence=0.79), 5)
    assert not d.accepted and "below minimum" in d.detail and not c.paused
    assert c.log[-1]["event"] == "halt_rejected"
    assert c.request_halt(req(confidence=0.80), 6).accepted


def test_reduce_exposure_is_recorded_for_the_policy_layer_and_trades_nothing():
    c = KillController(LIM)
    d = c.request_halt(req(action="REDUCE_EXPOSURE", target=None), 5)
    assert d.accepted and len(c.reduce_requests) == 1 and not c.paused and not c.engaged


def test_no_request_can_disengage_the_kill_switch_or_engage_it():
    c = KillController(LIM)
    c.observe(obs(orders_last_minute=9))
    for a, t in (("HALT_ALL", None), ("HALT_STRATEGY", "x_y"), ("REDUCE_EXPOSURE", None)):
        c.request_halt(req(action=a, target=t), 9)
    assert c.engaged
    clean = KillController(LIM)
    clean.request_halt(req(action="HALT_ALL", target=None), 9)
    assert not clean.engaged
    assert not hasattr(c, "disengage") and not hasattr(c, "place_order") and not hasattr(c, "cancel_order")


def test_resume_is_human_only_and_does_not_touch_other_pauses():
    c = KillController(LIM)
    c.request_halt(req(action="HALT_SYMBOL", target="ETHUSDT"), 1)
    c.request_halt(req(action="HALT_ALL", target=None), 2)
    with pytest.raises(ValueError, match="human"):
        c.resume(("all", "*"), "", 3)
    assert c.resume(("all", "*"), "owner", 4) is True
    assert c.paused_scopes == (("symbol", "ETHUSDT"),)
    assert c.resume(("all", "*"), "owner", 5) is False
    c.observe(obs(orders_last_minute=9)); c.reset("owner", 6)
    assert c.paused_scopes == (("symbol", "ETHUSDT"),)                  # a kill reset does not lift pauses


def test_events_go_to_a_hash_chained_sink(tmp_path):
    sink = ShadowChain(tmp_path, "v2kill", "controller", T0)
    c = KillController(LIM, sink)
    c.observe(obs(now_ms=T0 + 1, orders_last_minute=9))
    c.request_halt(req(), T0 + 2)
    c.reset("owner", T0 + 3)
    assert verify(tmp_path / "v2kill" / "controller") == {"ok": True, "records": 3,
                                                          "head": c.sink.head, "error": None}


def test_controller_is_pure_no_clock_io_or_order_path():
    src = pathlib.Path("src/trading/v2/killswitch.py").read_text()
    names = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            names |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            names.add((n.module or "").split(".")[0])
    assert names.isdisjoint({"time", "random", "socket", "requests", "os", "subprocess", "logging", "ccxt"})
    assert ".now(" not in src and "create_order" not in src and "cancel_order" not in src


# ------------------------------------------------------------ pauses in the risk engine

def test_a_pause_blocks_new_entries_in_its_scope_only():
    for scope in (("all", "*"), ("symbol", "BTCUSDT"), ("strategy", "btc_breakout_v3")):
        r = run(c=ctx(paused=(scope,)))
        assert not r.approved and r.reject_reason == "halt_scope" and failed(r) == ["halt_scope"]
        assert "paused by" in next(x.detail for x in r.checks if x.name == "halt_scope")
    for scope in (("symbol", "ETHUSDT"), ("strategy", "other_strategy")):
        assert run(c=ctx(paused=(scope,))).approved
    r = run()
    assert next(x for x in r.checks if x.name == "halt_scope").passed and r.approved


def test_a_pause_never_blocks_an_exit_and_the_check_is_still_listed():
    r = run(p=exit_proposal("exit"), c=ctx(state=state(**LONG_POS), paused=(("all", "*"),)))
    assert r.approved and r.size == 0.5
    assert next(x.detail for x in r.checks if x.name == "halt_scope").startswith("n/a")


# ------------------------------------------------------------ the pipeline with a controller

def test_a_latched_kill_blocks_every_entry_and_cancels_resting_paper_orders(tmp_path):
    ctl = KillController(LIM)
    ctl.observe(obs(orders_last_minute=9))
    p, bk = paper_pipeline(tmp_path, kill=False)
    p.provider._kill = lambda: ctl.engaged                              # the provider reads the controller
    bk.submit(cmd("77"), T0 + 10**9)                                    # a resting order from an earlier cycle
    assert len(bk.pending) == 1
    recs = p.run(make_bars())
    live = [r for r in recs if r["outcome"] != "no_snapshot"]
    assert {r["outcome"] for r in live} == {"risk_rejected"} and not bk.positions
    assert bk.pending == [] and bk.stats["cancelled:kill_switch"] == 1
    assert sum(r.get("cancelled_pending", 0) for r in recs) == 1 and p.stats["paper_cancelled_by_kill_switch"] == 1
    assert all(r["risk"]["reject_reason"] == "kill_switch" for r in live)


def test_a_hermes_halt_request_blocks_entries_through_the_provider(tmp_path):
    ctl = KillController(LIM)
    ctl.request_halt(req(action="HALT_SYMBOL", target="BTCUSDT"), 1)
    p, bk = paper_pipeline(tmp_path)
    p.provider._paused = lambda: ctl.paused_scopes
    live = [r for r in p.run(make_bars()) if r["outcome"] != "no_snapshot"]
    assert {r["outcome"] for r in live} == {"risk_rejected"} and bk.stats["queued"] == 0
    assert all(r["risk"]["reject_reason"] == "halt_scope" for r in live)


def test_paper_book_cancel_pending_directly():
    bk = PaperBook(1000.0, FEE)
    bk.submit(cmd("1"), T0 + 5 * M)
    bk.submit(cmd("2"), T0 + 6 * M)
    assert bk.cancel_pending(T0, "kill_switch") == 2 and bk.pending == [] and bk.stats["cancelled:kill_switch"] == 2
    assert bk.cancel_pending(T0, "kill_switch") == 0
