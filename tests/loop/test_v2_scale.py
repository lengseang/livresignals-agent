"""rc4: scaling from 3 to 15 symbols. Concurrent network phases, sequential decisions, a Jev call budget, presets."""
import json
import threading

import pytest

from trading.shadow.chain import verify
from trading.v2.adapter import JevAdapter, JevBalanceExhausted
from trading.v2.binance_public import FeedHttpError, FeedError, PrefetchedBook
from trading.v2.budget import BudgetedClient, JevBudget, JevBudgetExceeded
from trading.v2.daily_context import PRESETS, UNIVERSE, resolve_symbols
from trading.v2.features import Book
from trading.v2.fallback import StaticSource
from trading.v2.contracts import FALLBACK_STRATEGIES
from trading.v2.gateway import Gateway
from trading.v2.runner import Runner, RunnerConfig, demo_config
from trading.v2.workflow import load_active

from test_v2_runner import BASE, M, Client, Clock, FakeFeed, LONG_RAW, WF2

ALL15 = UNIVERSE


def mk_runner(tmp_path, symbols, feed_cls=FakeFeed, start_after_min=120, reply=LONG_RAW, **over):
    clock = Clock(BASE + start_after_min * M)
    feed = feed_cls(clock, symbols=symbols)
    c = RunnerConfig(**{**demo_config(symbols, 15, tmp_path).__dict__, **over})
    return Runner(c, feed, WF2, jev_client=Client(reply), position_client=Client(reply), clock=clock.time,
                  sleep=clock.sleep), feed, clock


# ------------------------------------------------------------ concurrency, proven rather than assumed

class BarrierFeed(FakeFeed):
    """klines('1m') blocks until EVERY symbol's request is in flight at once: it can only pass if the runner fetches
    concurrently (sequential fetching waits on the barrier for the timeout and the call raises BrokenBarrierError)."""

    def __init__(self, clock, symbols, **kw):
        super().__init__(clock, symbols=symbols, **kw)
        self.barrier = threading.Barrier(len(symbols), timeout=5)
        self.armed = False

    def klines(self, sym, interval, limit=500, end_time_ms=None, now_ms=None):
        if self.armed and interval == "1m":
            self.barrier.wait()
        return super().klines(sym, interval, limit, end_time_ms, now_ms)


def test_every_symbols_bars_are_fetched_concurrently(tmp_path):
    syms = ALL15[:6]
    r, feed, _ = mk_runner(tmp_path, syms, BarrierFeed)
    r.prime()
    feed.armed = True
    r.step()                                    # would raise BrokenBarrierError if the fetches were one after another
    assert feed.barrier.n_waiting == 0


def test_priming_context_and_stances_are_concurrent_too(tmp_path):
    syms = ALL15[:5]

    class PrimeBarrier(FakeFeed):
        def __init__(self, clock, symbols, **kw):
            super().__init__(clock, symbols=symbols, **kw)
            self.barrier = threading.Barrier(len(symbols), timeout=5)

        def history(self, sym, interval, n, now_ms=None):
            if interval == "1d" and sym != "BTCUSDT":
                self.barrier.wait()
            return super().history(sym, interval, n, now_ms)

    r, feed, _ = mk_runner(tmp_path, syms, PrimeBarrier)
    # stances need the daily history of every traded symbol except BTCUSDT's shared one: 4 of the 5 wait together
    feed.barrier = threading.Barrier(4, timeout=5)
    r.prime()
    assert r.stances.refreshed_ms is not None


# ------------------------------------------------------------ 15 symbols: all usable, deterministic, isolated failures

def test_fifteen_symbols_all_get_a_usable_decision_every_cycle(tmp_path):
    r, feed, clock = mk_runner(tmp_path, ALL15)
    r.run(max_steps=46)
    hb = json.loads((tmp_path / "heartbeat.json").read_text())
    assert hb["cycles"] == 45 and set(hb["by_symbol"]) == set(ALL15)
    for s in ALL15:
        assert hb["by_symbol"][s] == {"cycles": 3, "unusable": 0, "no_snapshot": 0}, s
    assert hb["timing"]["stale_ms_max"] <= hb["timing"]["stale_ceiling_ms"]
    assert hb["feed_failures"] == 0 and verify(tmp_path / "v2cycle" / "pipeline")["ok"]


def _strip(o):
    """Drop only what is legitimately wall-clock or derived from it: the adapter's measured latency and the chain hashes."""
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items() if k not in ("latency_ms", "hash", "prev_hash")}
    return [_strip(x) for x in o] if isinstance(o, list) else o


def test_a_run_is_deterministic_despite_the_threads(tmp_path):
    from trading.v2.paper import read_chain
    runs, heads = [], []
    for k in (1, 2):
        d = tmp_path / str(k)
        r, feed, clock = mk_runner(d, ALL15[:6])
        r.run(max_steps=46)
        runs.append(_strip(read_chain(d / "v2cycle" / "pipeline")))
        heads.append(verify(d / "v2paper" / "book")["head"])
    assert len(runs[0]) == 18 and runs[0] == runs[1]          # every decision record identical, in the same order
    assert heads[0] == heads[1]                                 # the paper book's chain is byte-identical


def test_one_failing_symbol_does_not_stop_the_others(tmp_path):
    class Flaky(FakeFeed):
        def history(self, sym, interval, n, now_ms=None):                  # priming works; only the live polls fail
            return FakeFeed.klines(self, sym, interval, n, now_ms=now_ms)

        def klines(self, sym, interval, limit=500, end_time_ms=None, now_ms=None):
            if sym == "SOLUSDT":
                raise FeedHttpError("503")
            return super().klines(sym, interval, limit, end_time_ms, now_ms)

    syms = ALL15[:5]
    r, feed, clock = mk_runner(tmp_path, syms, Flaky)
    r.run(max_steps=46)
    hb = json.loads((tmp_path / "heartbeat.json").read_text())
    assert hb["feed_failures"] >= 40                                  # SOL failed every step
    for s in syms:
        assert hb["by_symbol"][s]["cycles"] == (0 if s == "SOLUSDT" else 3), s


def test_a_non_feed_error_in_a_thread_is_not_swallowed(tmp_path):
    class Boom(FakeFeed):
        def klines(self, sym, interval, limit=500, end_time_ms=None, now_ms=None):
            if getattr(self, "boom", False) and sym == "ETHUSDT":
                raise RuntimeError("bug")
            return super().klines(sym, interval, limit, end_time_ms, now_ms)

    r, feed, _ = mk_runner(tmp_path, ALL15[:3], Boom)
    r.prime()
    feed.boom = True
    with pytest.raises(RuntimeError, match="bug"):
        r.step()


def test_heartbeat_reports_timing_and_prefetch(tmp_path):
    r, feed, _ = mk_runner(tmp_path, ALL15[:3])
    r.run(max_steps=46)
    hb = json.loads((tmp_path / "heartbeat.json").read_text())
    t = hb["timing"]
    assert t["step_ms_max"] >= t["step_ms_last"] >= 0 and "fetch_ms_last" in t
    assert hb["book_prefetch"]["hit"] >= 3 and hb["jev_budget"] is None


# ------------------------------------------------------------ the prefetched book

class StubFeed:
    def __init__(self):
        self.t, self.fail, self.fetches = 1_000_000, False, 0

    def now_ms(self):
        return self.t

    def book(self, sym):
        self.fetches += 1
        if self.fail:
            raise FeedHttpError("x")
        return Book(self.t, 1.0, 1.1, 1.0, 1.0)


def test_prefetched_book_is_used_once_and_only_while_fresh():
    f = StubFeed()
    b = PrefetchedBook(f, max_age_ms=2000)
    b.prefetch("BTCUSDT")
    f.t += 1500
    assert b.latest("BTCUSDT").ts_ms == 1_000_000 and f.fetches == 1 and b.stats["hit"] == 1
    b.latest("BTCUSDT")                                    # popped: a second use fetches fresh, never reuses
    assert f.fetches == 2 and b.stats["miss"] == 1
    b.prefetch("BTCUSDT"); f.t += 2500
    assert b.latest("BTCUSDT").ts_ms == f.t and b.stats["stale_refetch"] == 1


def test_prefetch_failure_means_no_book_never_an_invented_one():
    f = StubFeed(); f.fail = True
    b = PrefetchedBook(f)
    b.prefetch("BTCUSDT")
    assert b.latest("BTCUSDT") is None


# ------------------------------------------------------------ Jev call budget

def test_budget_counts_caps_and_rolls_over_at_utc_midnight():
    t = [BASE / 1000.0]
    bud = JevBudget(3, clock=lambda: t[0])
    assert [bud.take() for _ in range(5)] == [True, True, True, False, False]
    assert bud.snapshot()["used"] == 3 and bud.snapshot()["refused"] == 2
    t[0] += 86_400
    assert bud.take() and bud.snapshot() == {"day": bud.snapshot()["day"], "used": 1, "cap": 3, "refused": 0}


def test_budget_rejects_nonsense_caps():
    for bad in (0, -1, 1.5, "10", None):
        with pytest.raises(ValueError):
            JevBudget(bad)


def test_budgeted_client_refuses_without_calling_the_inner_client():
    calls = []

    class Inner:
        usage = {"calls": 0}

        def evaluate(self, request, timeout_s):
            calls.append(1)
            return {"ok": 1}
    c = BudgetedClient(Inner(), JevBudget(1))
    assert c.evaluate({}, 1.0) == {"ok": 1} and c.usage == {"calls": 0}
    with pytest.raises(JevBudgetExceeded) as e:
        c.evaluate({}, 1.0)
    assert isinstance(e.value, JevBalanceExhausted) and len(calls) == 1


def test_an_exhausted_budget_makes_the_gateway_answer_labelled_balance_exhausted():
    from test_v2_gateway import snap, state
    wf = load_active()

    class Inner:
        def evaluate(self, request, timeout_s):
            raise AssertionError("must not be called")
    ad = JevAdapter(BudgetedClient(Inner(), JevBudget(1)), wf, StaticSource({n: "LONG" for n in FALLBACK_STRATEGIES}),
                    timeout_s=1.0, clock=lambda: 0.0, gateway=Gateway())
    ad.client.budget.take()                                # spend the only call
    d = ad.evaluate(state(), snap())
    assert d.fallback_strategy == "system_one_gateway" and d.fallback_reason.startswith("balance_exhausted")


def test_runner_budget_shared_by_entry_and_position_clients(tmp_path):
    r, feed, clock = mk_runner(tmp_path, ALL15[:3], jev_daily_call_cap=4)
    r.run(max_steps=46)
    hb = json.loads((tmp_path / "heartbeat.json").read_text())
    assert hb["jev_budget"]["used"] == 4 and hb["jev_budget"]["refused"] > 0
    assert hb["jev_ok"] <= 4 and hb["fallback_gateway"] > 0           # the gateway carried the rest, labelled


def test_approved_config_carries_a_daily_cap_with_headroom():
    from pathlib import Path
    from trading.v2.approved import JEV_DAILY_CALL_CAP, approved_config
    c = approved_config(ALL15, 15, Path("/tmp/x"))
    assert c.jev_daily_call_cap == JEV_DAILY_CALL_CAP >= int(1.3 * 2 * 15 * 96)       # worst case entries + positions


# ------------------------------------------------------------ presets

def test_presets_and_symbol_resolution():
    assert resolve_symbols("top3") == ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    assert len(resolve_symbols("top8")) == 8 and resolve_symbols("all15") == ALL15
    assert resolve_symbols("top3, btcusdt, ltcusdt") == ("BTCUSDT", "ETHUSDT", "SOLUSDT", "LTCUSDT")
    assert PRESETS["top3"] == ALL15[:3] and set(PRESETS["top8"]) <= set(ALL15)
    for bad in ("", " , ", "BTC/USDT", "ab", "BTC USDT"):
        with pytest.raises(ValueError):
            resolve_symbols(bad)


def test_cli_accepts_presets_and_refuses_junk(tmp_path, monkeypatch):
    import trading.v2.runner as R
    seen = {}

    class FakeRunner:
        def __init__(self, cfg, feed, wf, **kw):
            seen["cfg"] = cfg

        def run(self, **kw):
            (seen["cfg"].out_dir / "heartbeat.json").write_text("{}")
    monkeypatch.setattr(R, "Runner", FakeRunner)
    monkeypatch.setattr(R, "BinancePublic", lambda: object())
    assert R.main(["runner", "--approved", "--symbols", "all15", "--out", str(tmp_path)]) == 0
    assert seen["cfg"].symbols == ALL15 and seen["cfg"].kill_limits.max_orders_per_minute == 30
    assert R.main(["runner", "--approved", "--symbols", "BTC/USDT", "--out", str(tmp_path)]) == 2


def test_preflight_takes_presets(tmp_path, monkeypatch):
    from trading.v2 import preflight
    monkeypatch.setenv("TYPESAFE_API_KEY", "x")
    assert preflight.main(["--out", str(tmp_path), "--symbols", "all15", "--offline"]) == 0
    assert preflight.main(["--out", str(tmp_path), "--symbols", "nope!", "--offline"]) == 2


def test_decisions_are_made_in_the_configured_symbol_order(tmp_path):
    from trading.v2.paper import read_chain
    syms = ("SOLUSDT", "BTCUSDT", "LTCUSDT", "ETHUSDT")             # deliberately not alphabetical
    r, feed, clock = mk_runner(tmp_path, syms)
    r.run(max_steps=46)
    by_ts: dict = {}
    for rec in read_chain(tmp_path / "v2cycle" / "pipeline"):
        by_ts.setdefault(rec["ts_event_ms"], []).append(rec["symbol"])
    assert len(by_ts) == 3 and all(order == list(syms) for order in by_ts.values())
