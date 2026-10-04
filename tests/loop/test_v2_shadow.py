"""V2-6: the shadow pipeline. Nothing executes; everything is logged, chained and replayable."""
import ast
import json
import pathlib

import pytest

from trading.replay.bars import Bar
from trading.shadow.chain import ShadowChain, verify
from trading.v2.adapter import JevAdapter, JevHttpError
from trading.v2.contracts import (FALLBACK_STRATEGIES, Decision, DecisionState, ExecutionCommand,
                                  PolicyDecision, PositionState, RiskDecision, RiskState, StrategyRef,
                                  TradeProposal)
from trading.v2.fallback import StaticSource
from trading.v2.features import Book
from trading.v2.policy import ExitParams
from trading.v2.risk import ExchangeHealth, RiskLimits
from trading.v2.shadow import CycleContext, ShadowPipeline, compare_with_legacy
from trading.v2.workflow import load_active

from test_v2_adapter import raw_ok
from test_v2_features import T0, series

SYM = "BTCUSDT"
WF = load_active()
M = 60_000
H = 3_600_000
LONG_RAW = raw_ok()
LONG_RAW["answers"]["action"] = {"value": "LONG", "probability": 0.95, "distribution": {
    "LONG": 0.95, "SHORT": 0.01, "HOLD": 0.02, "EXIT": 0.005, "REDUCE": 0.005, "NO_TRADE": 0.01}}


def make_bars(n1=300, n1h=130):
    m1 = []
    for i, c in enumerate(series(n1, seed=3)):
        o = T0 + i * M
        m1.append(Bar(SYM, "1m", o, o + M, c, c * 1.001, c * 0.999, c, 10 + (i * 7 % 13),
                      c * (10 + (i * 7 % 13)), 5, 5.0))
    last_end = m1[-1].end_ms
    h1 = []
    for j, c in enumerate(series(n1h, seed=5, noise=0.01)):
        end = last_end - (n1h - 1 - j) * H
        h1.append(Bar(SYM, "1h", end - H, end, c, c * 1.003, c * 0.997, c, 500.0, c * 500.0, 100, 250.0))
    return m1 + h1


class FollowBook:
    """A book whose timestamp tracks the pipeline's newest 1m bar (what a live recorder would give)."""
    def __init__(self, bid_qty=12.8, ask_qty=9.7, spread=0.5):
        self.pipeline, self.bq, self.aq, self.spread = None, bid_qty, ask_qty, spread

    def latest(self, symbol):
        t = self.pipeline.store.tail(symbol, "1m", 1)
        return Book(t[-1].end_ms, 100000.0, 100000.0 + self.spread, self.bq, self.aq) if t else None


class Ctx:
    def __init__(self, **over):
        self.base = dict(
            position=PositionState(side="none", size=0, entry_price=None, unrealized_pnl=0),
            risk_state=RiskState(equity=10000, daily_pnl=-72, daily_loss_limit_remaining=428, current_exposure=0.0),
            strategy=StrategyRef(name="btc_breakout_v3", version="3.2.1"), exit_params=ExitParams(0.02, 0.04),
            day_start_equity=10000.0, week_start_equity=10000.0, high_water_mark=10000.0,
            exchange=ExchangeHealth(True, "REST 200, 91ms"), kill_switch_engaged=False)
        self.base.update(over)
        self.calls = 0

    def context(self, symbol, as_of_ms):
        self.calls += 1
        return CycleContext(**self.base)


class Client:
    def __init__(self, reply):
        self.reply = reply

    def evaluate(self, request, timeout_s):
        if isinstance(self.reply, Exception):
            raise self.reply
        return json.loads(json.dumps(self.reply))


def build(tmp_path, reply=LONG_RAW, ctx=None, book=True, fallback=None, cadence=15, sink=None, **kw):
    adapter = JevAdapter(Client(reply), WF, fallback or StaticSource({n: "LONG" for n in FALLBACK_STRATEGIES}),
                         timeout_s=1.0, clock=lambda: 0.0)
    fb = FollowBook() if book is True else (book or None)
    sink = sink or ShadowChain(tmp_path, "v2shadow", "pipeline", T0)
    p = ShadowPipeline(adapter=adapter, provider=ctx or Ctx(), sink=sink, book=fb,
                       limits=RiskLimits(daily_loss_cap_usd=100.0), cadence_minutes=cadence, **kw)
    if isinstance(fb, FollowBook):
        fb.pipeline = p
    return p


def outcomes(recs):
    return [r["outcome"] for r in recs]


# ------------------------------------------------------------ the happy chain, end to end

def test_full_replay_logs_every_cycle_honestly(tmp_path):
    p = build(tmp_path)
    recs = p.run(make_bars())
    assert len(recs) == 20 and p.stats["cycles"] == 20 and p.stats["records"] == 20
    o = outcomes(recs)
    assert o.count("no_snapshot") == 16 and o.count("would_execute") == 4        # k=15..240 lack 241 bars
    first = recs[0]
    assert any("returns(need 241" in m for m in first["missing"])
    assert p.summary()["outcome:would_execute"] == 4 and p.summary()["jev_share"] == 1.0
    assert verify(tmp_path / "v2shadow" / "pipeline") == {
        "ok": True, "records": 20, "head": recs[-1]["hash"], "error": None}


def test_a_would_execute_record_contains_the_whole_reconstructable_chain(tmp_path):
    rec = [r for r in build(tmp_path).run(make_bars()) if r["outcome"] == "would_execute"][-1]
    assert rec["mode"] == "shadow" and rec["real"] is False and "not real money" in rec["label"]
    state = DecisionState.from_dict(rec["state"])
    assert state.state_hash() == rec["state_hash"]
    dec = Decision.from_dict(rec["decision"])
    assert dec.source == "jev" and dec.state_hash == rec["state_hash"] and dec.action == "LONG"
    pol = PolicyDecision.from_dict(rec["policy"])
    assert pol.tier == "EXECUTE" and pol.decision_id == dec.decision_id and rec["policy_reasons"] == []
    prop = TradeProposal.from_dict(rec["proposal"])
    risk = RiskDecision.from_dict(rec["risk"])
    assert risk.approved and risk.proposal_id == prop.proposal_id and len(risk.checks) >= 14
    cmd = ExecutionCommand.from_dict(rec["execution_preview"])
    assert cmd.size == risk.size and cmd.risk_decision_id == risk.risk_decision_id and cmd.reduce_only is False
    assert rec["versions"] == {"feature": "features-v1", "workflow": "crypto_trading-v1", "policy": "2.0.0",
                               "risk": "risk-v2.0", "model": "jev-latest"}
    assert rec["event"]["kind"] == "kline_closed" and rec["event"]["timeframe"] == "1m"
    assert rec["features"]["snapshot_id"].startswith("feat_") and rec["comparison"]["kind"] == "no_legacy"


def test_two_identical_runs_produce_identical_chains(tmp_path):
    a = build(tmp_path / "a").run(make_bars())
    b = build(tmp_path / "b").run(make_bars())
    assert [r["hash"] for r in a] == [r["hash"] for r in b]


# ------------------------------------------------------------ every way a cycle can end

def test_jev_outage_means_escalation_never_execution(tmp_path):
    p = build(tmp_path, reply=JevHttpError("down"))
    recs = p.run(make_bars())
    live = [r for r in recs if r["outcome"] != "no_snapshot"]
    assert len(live) == 4 and set(outcomes(live)) == {"escalate"}
    assert all(r["decision"]["source"] == "fallback" and r["decision"]["confidence"] <= 0.89 for r in live)
    assert all(r["proposal"] is None and r["risk"] is None and r["execution_preview"] is None for r in live)
    assert p.stats["outcome:would_execute"] == 0 and p.summary()["jev_share"] == 0.0
    assert p.adapter.stats["fallback:http_error"] == 4


def test_missing_book_logs_no_snapshot_with_the_reason(tmp_path):
    recs = build(tmp_path, book=False).run(make_bars())
    assert set(outcomes(recs)) == {"no_snapshot"} and len(recs) == 20
    assert any("orderbook(none)" in m for m in recs[-1]["missing"])


def test_kill_switch_engaged_is_a_risk_rejection_with_the_failed_check_recorded(tmp_path):
    recs = build(tmp_path, ctx=Ctx(kill_switch_engaged=True)).run(make_bars())
    live = [r for r in recs if r["outcome"] != "no_snapshot"]
    assert set(outcomes(live)) == {"risk_rejected"}
    risk = RiskDecision.from_dict(live[-1]["risk"])
    assert not risk.approved and risk.reject_reason == "kill_switch" and live[-1]["execution_preview"] is None


def test_unknown_exchange_health_fails_closed(tmp_path):
    live = [r for r in build(tmp_path, ctx=Ctx(exchange=None)).run(make_bars()) if r["outcome"] != "no_snapshot"]
    assert set(outcomes(live)) == {"risk_rejected"} and live[-1]["risk"]["reject_reason"] == "exchange_health"


@pytest.mark.parametrize("action,conf,expected", [("HOLD", 0.97, "ignored"), ("LONG", 0.60, "watch"),
                                                  ("LONG", 0.80, "escalate")])
def test_policy_tiers_end_the_cycle_without_a_proposal(tmp_path, action, conf, expected):
    raw = json.loads(json.dumps(LONG_RAW))
    rest = {a: (1 - conf) / 5 for a in ("LONG", "SHORT", "HOLD", "EXIT", "REDUCE", "NO_TRADE") if a != action}
    raw["answers"]["action"] = {"value": action, "probability": conf, "distribution": {action: conf, **rest}}
    live = [r for r in build(tmp_path, reply=raw).run(make_bars()) if r["outcome"] != "no_snapshot"]
    assert set(outcomes(live)) == {expected} and all(r["proposal"] is None for r in live)


def test_weak_entry_quality_is_demoted_and_the_reason_is_logged(tmp_path):
    raw = json.loads(json.dumps(LONG_RAW)); raw["answers"]["entry_quality"]["value"] = 0.55
    live = [r for r in build(tmp_path, reply=raw).run(make_bars()) if r["outcome"] != "no_snapshot"]
    assert set(outcomes(live)) == {"escalate"} and live[-1]["policy_reasons"][0].startswith("entry_quality_0.55")


def test_missing_exit_params_yield_no_proposal(tmp_path):
    live = [r for r in build(tmp_path, ctx=Ctx(exit_params=None)).run(make_bars()) if r["outcome"] != "no_snapshot"]
    assert set(outcomes(live)) == {"no_proposal"} and live[-1]["proposal_reason"] == "exit_params_unknown"


class LateBook(FollowBook):
    """A fresh book (stamped at receive time) beside bars that arrived a minute late."""
    def latest(self, symbol):
        t = self.pipeline.store.tail(symbol, "1m", 1)
        return Book(t[-1].end_ms + 60_000, 100000.0, 100000.5, 12.8, 9.7) if t else None


def test_stale_snapshot_is_recorded_as_unusable(tmp_path):
    p = build(tmp_path, book=LateBook())
    p.book.pipeline = p
    recs = [p.on_bar(b, b.end_ms + 60_000) for b in sorted(make_bars(), key=lambda b: (b.end_ms, -(b.end_ms - b.open_ms)))]
    recs = [r for r in recs if r]
    unusable = [r for r in recs if r["outcome"] == "snapshot_unusable"]
    assert len(unusable) == 4 and "stale" in unusable[0]["reason"]      # 60s late > max_stale_ms 10s
    assert all(r["features"]["data_quality"]["stale_ms"] == 60_000 for r in unusable)
    assert p.stats["outcome:would_execute"] == 0


def test_a_stale_order_book_blocks_the_snapshot_first(tmp_path):
    p = build(tmp_path)
    recs = [r for r in (p.on_bar(b, b.end_ms + 60_000) for b in
                        sorted(make_bars(), key=lambda b: (b.end_ms, -(b.end_ms - b.open_ms)))) if r]
    assert set(outcomes(recs)) == {"no_snapshot"}
    assert any("orderbook(stale" in m for m in recs[-1]["missing"])


def test_a_crash_inside_a_cycle_is_recorded_not_lost_and_the_next_cycle_still_runs(tmp_path):
    class Flaky(Ctx):
        def context(self, symbol, as_of_ms):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("paper book unreadable")
            return super().context(symbol, as_of_ms)
    p = build(tmp_path, ctx=Flaky())
    recs = p.run(make_bars())
    errs = [r for r in recs if r["outcome"] == "cycle_error"]
    assert len(errs) == 1 and errs[0]["stage"] == "context" and errs[0]["error"] == "RuntimeError"
    assert p.stats["cycle_error"] == 1 and p.stats["outcome:would_execute"] == 3
    assert verify(tmp_path / "v2shadow" / "pipeline")["ok"]


def test_sink_failure_is_loud_never_swallowed(tmp_path):
    class Dead:
        def write(self, rec):
            raise OSError("disk full")
    p = build(tmp_path, sink=Dead())
    with pytest.raises(OSError, match="disk full"):
        p.run(make_bars())
    assert p.stats["sink_error"] == 1 and p.stats["records"] == 0


# ------------------------------------------------------------ feed hygiene, restart, integrity

def test_duplicate_or_out_of_order_bars_are_rejected_and_counted(tmp_path):
    p = build(tmp_path)
    bars = sorted(make_bars(), key=lambda b: (b.end_ms, -(b.end_ms - b.open_ms)))
    for b in bars[:20]:
        p.on_bar(b, b.end_ms)
    n = p.stats["bars"]
    assert p.on_bar(bars[19], bars[19].end_ms) is None and p.stats["bar_rejected"] == 1
    assert p.on_bar(bars[3], bars[3].end_ms) is None and p.stats["bar_rejected"] == 2
    assert p.stats["bars"] == n


def test_restart_replays_history_without_logging_a_cycle_twice(tmp_path):
    first = build(tmp_path)
    recs = first.run(make_bars())
    again = build(tmp_path)                                  # new process, same chain on disk
    assert again.resume_after_ms == recs[-1]["ts_event_ms"]
    assert again.run(make_bars()) == []
    assert again.stats["skipped_already_logged"] == 20 and again.stats["records"] == 0
    assert verify(tmp_path / "v2shadow" / "pipeline")["records"] == 20


def test_chain_detects_tampering_with_a_logged_decision(tmp_path):
    build(tmp_path).run(make_bars())
    day = sorted((tmp_path / "v2shadow" / "pipeline").glob("*.jsonl"))[0]
    text = day.read_text()
    assert '"outcome":"would_execute"' in text
    day.write_text(text.replace('"outcome":"would_execute"', '"outcome":"risk_rejected"', 1))
    res = verify(tmp_path / "v2shadow" / "pipeline")
    assert not res["ok"] and "altered" in res["error"]


def test_cadence_controls_when_decisions_are_made(tmp_path):
    p = build(tmp_path, cadence=60)
    recs = p.run(make_bars())
    assert [r["ts_event_ms"] for r in recs] == [T0 + k * M for k in (60, 120, 180, 240, 300)]
    assert outcomes(recs) == ["no_snapshot"] * 4 + ["would_execute"]
    with pytest.raises(ValueError):
        build(tmp_path / "x", cadence=0)


# ------------------------------------------------------------ comparison with the current system

def test_comparison_with_legacy_decisions():
    assert compare_with_legacy("LONG", "LONG")["kind"] == "agree"
    assert compare_with_legacy("LONG", "SHORT") == {"kind": "disagree", "v2": "LONG", "legacy": "SHORT"}
    assert compare_with_legacy("HOLD", "NO_TRADE")["kind"] == "agree"       # both inactive
    assert compare_with_legacy("EXIT", "HOLD")["kind"] == "disagree"
    assert compare_with_legacy("LONG", None)["kind"] == "no_legacy"


def test_disagreements_are_counted_in_the_heartbeat(tmp_path):
    p = build(tmp_path, ctx=Ctx(legacy_action="NO_TRADE"))
    p.run(make_bars())
    assert p.stats["compare:disagree"] == 4 and p.stats["compare:agree"] == 0
    p2 = build(tmp_path / "b", ctx=Ctx(legacy_action="LONG"))
    p2.run(make_bars())
    assert p2.stats["compare:agree"] == 4


# ------------------------------------------------------------ the shadow guarantee

def test_shadow_module_has_no_execution_path():
    src = pathlib.Path("src/trading/v2/shadow.py").read_text()
    names = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            names |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            names.add(n.module or "")
    forbidden = ("live_client", "live_executor", "futures_executor", "trend_follower", "unified_trader",
                 "manager", "ccxt", "binance", "requests", "httpx", "socket", "subprocess", "os")
    assert not [m for m in names if any(f == m.split(".")[-1] or m.startswith(f + ".") for f in forbidden)], names
    assert "place_order" not in src and "create_order" not in src


def test_every_cycle_is_labelled_shadow_and_never_real(tmp_path):
    for r in build(tmp_path).run(make_bars()):
        assert r["mode"] == "shadow" and r["real"] is False and "SHADOW" in r["label"]
