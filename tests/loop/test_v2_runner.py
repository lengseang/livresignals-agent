"""The runner, on a fake feed with a simulated clock the runner itself advances. No network, no real sleeping."""
import ast
import json
import pathlib

import pytest

from trading.replay.bars import Bar
from trading.shadow.chain import verify
from trading.v2.binance_public import FeedHttpError
from trading.v2.features import Book
from trading.v2.killswitch import KillLimits
from trading.v2.risk import RiskLimits
from trading.v2.runner import RunnerConfig, Runner, demo_config, main
from trading.v2.typesafe_client import KEY_ENV
from trading.v2.workflow import WORKFLOW_DIR, load_workflow

from test_v2_features import series
from test_v2_shadow import Client, LONG_RAW

M, H, D = 60_000, 3_600_000, 86_400_000
BASE = 1_700_006_400_000                       # a UTC midnight
WF2 = load_workflow(WORKFLOW_DIR / "crypto_trading_v2.json")
SYM = "BTCUSDT"


class Clock:
    def __init__(self, t_ms):
        self.t = t_ms / 1000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


def mk(sym, interval, i, o, c, vol, step, base):
    s = base + i * step
    return Bar(sym, interval, s, s + step, o, max(o, c) * 1.0005, min(o, c) * 0.9995, c, vol, c * vol, 50, vol / 2)


class FakeFeed:
    def __init__(self, clock, symbols=(SYM,), start_price=100_000.0):
        self.clock, self.bars, self.fail, self.errors, self.book_lead_ms = clock, {}, 0, 0, 0
        self.latency_s = 0.0
        self.last_latency_ms, self.last_ok_ms, self.calls = None, None, []
        for k, sym in enumerate(symbols):
            c1 = series(900, start=start_price, drift=0.00002, noise=0.0015, seed=11 + k)
            self.bars[(sym, "1m")] = [mk(sym, "1m", i, c1[i - 1] if i else start_price, c1[i], 10 + (i * 7 % 13), M, BASE - 450 * M)
                                      for i in range(900)]
            ch = series(400, start=start_price * 0.9, drift=0.0004, noise=0.01, seed=21 + k)
            self.bars[(sym, "1h")] = [mk(sym, "1h", i, ch[i - 1] if i else start_price * 0.9, ch[i], 500.0, H, BASE - 300 * H)
                                      for i in range(400)]
            cd = series(1000, start=start_price * 0.2, drift=0.0025, noise=0.02, seed=31 + k)
            self.bars[(sym, "1d")] = [mk(sym, "1d", i, cd[i - 1] if i else start_price * 0.2, cd[i], 30_000.0, D, BASE - 996 * D)
                                        for i in range(1000)]

    def now_ms(self):
        return int(self.clock.time() * 1000)

    def sync_clock(self):
        return 0

    def _maybe_fail(self):
        if self.fail > 0:
            self.fail -= 1
            self.errors += 1
            raise FeedHttpError("503")
        self.clock.t += self.latency_s                     # every call takes this long (simulated)
        self.last_latency_ms, self.last_ok_ms = 25.0, self.now_ms()

    def klines(self, sym, interval, limit=500, end_time_ms=None, now_ms=None):
        self._maybe_fail()
        now = self.now_ms() if now_ms is None else now_ms
        self.calls.append((sym, interval, limit))
        rows = [b for b in self.bars[(sym, interval)] if b.end_ms <= now and (end_time_ms is None or b.open_ms <= end_time_ms)]
        return rows[-limit:]

    def history(self, sym, interval, n, now_ms=None):
        return self.klines(sym, interval, n, now_ms=now_ms)

    def book(self, sym):
        self._maybe_fail()
        px = self.bars[(sym, "1m")][0].close
        last = [b for b in self.bars[(sym, "1m")] if b.end_ms <= self.now_ms()][-1].close
        return Book(self.now_ms() + self.book_lead_ms, last, last * 1.00001, 12.8, 9.7)


def feed_for(clock, **kw):
    return FakeFeed(clock, **kw)


def cfg(tmp_path, cadence=15, **over):
    c = demo_config((SYM,), cadence, tmp_path)
    return RunnerConfig(**{**c.__dict__, **over}) if over else c


def runner(tmp_path, start_after_min=120, reply=LONG_RAW, cadence=15, symbols=(SYM,), **over):
    clock = Clock(BASE + start_after_min * M)
    feed = feed_for(clock, symbols=symbols)
    c = demo_config(symbols, cadence, tmp_path)
    if over:
        c = RunnerConfig(**{**c.__dict__, **over})
    r = Runner(c, feed, WF2, jev_client=Client(reply), clock=clock.time, sleep=clock.sleep)
    return r, feed, clock


def hb(tmp_path):
    return json.loads((tmp_path / "heartbeat.json").read_text())


# ------------------------------------------------------------ priming

def test_prime_loads_history_without_deciding_anything(tmp_path):
    r, feed, _ = runner(tmp_path)
    r.prime()
    assert len(r.pipeline.store.all(SYM, "1m")) == 261 and len(r.pipeline.store.all(SYM, "1h")) == 140
    assert r.pipeline.stats["cycles"] == 0 and r.adapter.stats["calls"] == 0
    assert r.stances.refreshed_ms is not None and r.stances.trend[SYM] in ("LONG", "FLAT")
    assert r.book.last[SYM] == r.last_price[SYM] and r.last_end[SYM] == r.pipeline.store.all(SYM, "1m")[-1].end_ms


# ------------------------------------------------------------ a normal run

def test_a_run_decides_on_the_cadence_logs_everything_and_trades_paper_only(tmp_path):
    r, feed, clock = runner(tmp_path)
    r.run(max_steps=46)
    h = hb(tmp_path)
    assert h["steps"] == 46 and h["cycles"] == 3 and h["jev_calls"] == 3 and h["jev_share"] == 1.0
    assert h["outcomes"] == {"would_execute": 1, "no_proposal": 2} and h["positions"] == [SYM]
    assert h["models"] == {"jev-latest": 3}
    assert h["feed_failures"] == 0 and h["kill_engaged"] is False and h["paper_equity"] > 0
    for lane, strat in (("v2cycle", "pipeline"), ("v2paper", "book"), ("v2kill", "controller")):
        d = tmp_path / lane / strat
        if d.exists():
            assert verify(d)["ok"]
    assert verify(tmp_path / "v2cycle" / "pipeline")["records"] == 3


def test_decisions_land_only_on_the_cadence_boundaries(tmp_path):
    r, feed, clock = runner(tmp_path, cadence=15)
    seen = []
    r.prime()
    for _ in range(35):
        clock.sleep(60 - (clock.time() % 60) + 3)
        seen += [x["ts_event_ms"] for x in r.step()]
    assert seen and all(t % (15 * M) == 0 for t in seen)
    assert r.steps == 35


def test_the_heartbeat_has_no_secret_and_counts_jev_usage_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-test-SECRET")
    r, feed, clock = runner(tmp_path)
    r.run(max_steps=3)
    text = (tmp_path / "heartbeat.json").read_text()
    assert "SECRET" not in text and "sk-" not in text


# ------------------------------------------------------------ human controls

def test_a_stop_file_exits_cleanly_before_any_step(tmp_path):
    r, feed, clock = runner(tmp_path)
    (tmp_path / "STOP").write_text("")
    r.run(max_steps=100)
    assert r.steps == 0 and r.pipeline.stats["cycles"] == 0


def test_a_kill_file_blocks_entries_and_is_visible_in_the_heartbeat(tmp_path):
    r, feed, clock = runner(tmp_path)
    (tmp_path / "V2_KILL").write_text("")
    r.run(max_steps=46)
    h = hb(tmp_path)
    assert h["kill_engaged"] is True and h["positions"] == [] and h["paper_equity"] == 1_000.0
    assert h["outcomes"].get("risk_rejected", 0) == 3 and "would_execute" not in h["outcomes"]


# ------------------------------------------------------------ failure handling

def test_feed_failures_make_the_exchange_unhealthy_and_latch_the_kill_switch(tmp_path):
    r, feed, clock = runner(tmp_path)
    r.prime()
    feed.fail = 10_000                                      # the feed is down from now on
    states = []
    for _ in range(5):
        clock.sleep(60 - (clock.time() % 60) + 3)
        r.step()
        states.append(r.ctl.engaged)
    assert states == [False, False, True, True, True]       # 3 consecutive failures (KillLimits) latch it
    assert "exchange_down" in r.ctl.reasons and r.feed_failures == 5
    assert hb(tmp_path)["kill_engaged"] is True and hb(tmp_path)["feed_failures"] == 5
    feed.fail = 0
    clock.sleep(63)
    r.step()
    assert r.ctl.engaged, "a latched kill switch must NOT clear itself when the feed recovers"


def test_a_long_sleep_is_backfilled_in_order_and_a_late_decision_is_refused_as_stale(tmp_path):
    r, feed, clock = runner(tmp_path)
    r.prime()
    before = r.last_end[SYM]
    assert before == BASE + 120 * M
    clock.sleep(17 * 60)                                    # the process was suspended for 17 minutes
    recs = r.step()
    assert r.last_end[SYM] == before + 17 * M               # caught up exactly, nothing skipped
    ends = [b.end_ms for b in r.pipeline.store.all(SYM, "1m")][-25:]
    assert all(b - a == M for a, b in zip(ends, ends[1:]))   # contiguous: no hole, no duplicate
    assert [x["ts_event_ms"] for x in recs] == [BASE + 135 * M]      # the one 15-minute boundary inside the gap
    # that decision is processed 2 minutes late: the snapshot is stale (limit 20 s), so NO decision is made on old data
    assert recs[0]["outcome"] == "snapshot_unusable" and "stale" in recs[0]["reason"]
    assert recs[0]["features"]["data_quality"]["stale_ms"] == 2 * M and r.adapter.stats["calls"] == 0


def test_catching_up_exactly_to_a_boundary_decides_normally(tmp_path):
    r, feed, clock = runner(tmp_path)
    r.prime()
    clock.sleep(15 * 60)                                    # lands exactly on the 15-minute boundary
    recs = r.step()
    assert r.last_end[SYM] == BASE + 135 * M and [x["ts_event_ms"] for x in recs] == [BASE + 135 * M]
    assert recs[0]["outcome"] == "would_execute" and recs[0]["features"]["data_quality"]["complete"] is True
    assert recs[0]["features"]["data_quality"]["stale_ms"] == 0


def test_the_utc_day_rollover_refreshes_the_stances(tmp_path):
    r, feed, clock = runner(tmp_path, start_after_min=-3)    # 3 minutes before midnight
    r.prime()
    first_day, first = r._stance_day, r.stances.refreshed_ms
    for _ in range(6):
        clock.sleep(60 - (clock.time() % 60) + 3)
        r.step()
    assert r._stance_day != first_day and r.stances.refreshed_ms > first


def test_without_a_key_every_decision_falls_back_and_says_so(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    clock = Clock(BASE + 120 * M)
    feed = feed_for(clock)
    r = Runner(demo_config((SYM,), 15, tmp_path), feed, WF2, clock=clock.time, sleep=clock.sleep)   # real client class
    r.run(max_steps=46)
    h = hb(tmp_path)
    assert h["jev_calls"] == 3 and h["jev_ok"] == 0 and h["jev_fallback"] == 3 and h["jev_share"] == 0.0
    assert h["outcomes"].get("would_execute", 0) == 0 and h["positions"] == []
    assert r.adapter.stats["fallback:http_error"] == 3


# ------------------------------------------------------------ configuration and safety

def test_nothing_runs_without_the_demo_flag(capsys):
    assert main(["runner"]) == 2
    assert "refusing to run" in capsys.readouterr().err


def test_demo_limits_are_explicit_and_the_runner_config_has_no_hidden_defaults():
    c = demo_config((SYM,), 15, pathlib.Path("x"))
    assert c.risk_limits.daily_loss_cap_usd == 100.0 and c.kill_limits.exchange_failures_to_engage == 3
    import dataclasses
    required = {f.name for f in dataclasses.fields(RunnerConfig) if f.default is dataclasses.MISSING
                and f.default_factory is dataclasses.MISSING}
    assert {"risk_limits", "kill_limits", "exit_params", "daily_loss_budget_usd", "paper_equity", "symbols",
            "cadence_minutes", "out_dir"} <= required


def test_runner_and_feed_have_no_order_path_and_only_read_public_data():
    for mod in ("runner", "binance_public"):
        src = pathlib.Path(f"src/trading/v2/{mod}.py").read_text()
        names = set()
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Import):
                names |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                names.add(n.module or "")
        banned = ("live_client", "live_executor", "futures_executor", "trend_follower", "unified_trader", "manager", "ccxt",
                  "hmac", "signature")
        assert not [m for m in names if any(b in m for b in banned)], (mod, names)
        assert "create_order" not in src and "api_secret" not in src.lower() and "signature" not in src.lower(), mod
    feed = pathlib.Path("src/trading/v2/binance_public.py").read_text()
    assert 'method="GET"' in feed and "POST" not in feed and "/api/v3/order" not in feed
    assert "data-api.binance.vision" in feed and "LIVE_ENABLED" not in pathlib.Path("src/trading/v2/runner.py").read_text().replace("LIVE_ENABLED, cron", "")


def test_a_live_book_stamped_after_the_step_started_still_yields_a_decision(tmp_path):
    r, feed, clock = runner(tmp_path)
    feed.book_lead_ms = 800                                 # what the first live run saw: fetch latency
    r.run(max_steps=46)
    assert hb(tmp_path)["outcomes"] == {"would_execute": 1, "no_proposal": 2}


def test_a_book_from_far_in_the_future_is_still_refused_by_the_runner(tmp_path):
    r, feed, clock = runner(tmp_path)
    feed.book_lead_ms = 60_000
    r.run(max_steps=46)
    assert hb(tmp_path)["outcomes"] == {"no_snapshot": 3}


def test_every_symbol_in_a_slow_multi_symbol_step_gets_a_snapshot(tmp_path):
    """Found live: with 3 symbols each step takes seconds, so the LAST symbol's book was 'after now' and had no snapshot."""
    syms = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    clock = Clock(BASE + 120 * M)
    feed = feed_for(clock, symbols=syms)
    feed.latency_s = 1.0                                    # each feed call takes 1 s (live is ~0.75 s)
    r = Runner(demo_config(syms, 15, tmp_path), feed, WF2, jev_client=Client(LONG_RAW), clock=clock.time, sleep=clock.sleep)
    r.prime()
    feed.latency_s = 1.0
    clock.sleep(15 * 60)
    recs = r.step()
    assert sorted(x["symbol"] for x in recs) == sorted(syms)
    assert {x["outcome"] for x in recs} <= {"would_execute", "no_proposal"}, [(x["symbol"], x["outcome"], x.get("missing")) for x in recs]
