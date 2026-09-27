"""trading.scanner: live ticks -> 1m bars -> the SAME strategy loop as replay (paper only).

Design: docs/loop/designs/T3.2.md. The key test feeds one bar history through the replay engine and,
as ticks, through the live path, and requires identical signals. Every signal here is simulated.
"""
from __future__ import annotations

import gzip
import heapq
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading.replay import bars as B
from trading.replay import loader, windows
from trading.replay.broker import CostModel, PaperAccount
from trading.replay.engine import replay
from trading.replay.plugins.trend_sma import TrendSMA
from trading.replay.strategy import Signal
from trading.scanner import registry as R
from trading.scanner.bars import BarBuilder, ticks_from_bar
from trading.scanner.scanner import Scanner
from trading.scanner.sink import DailySink
from trading.stream.normalize import Tick, parse

MIN = 60_000
DAY = 86_400_000
SEC = 1_000
REPO = Path(__file__).resolve().parents[2]
HISTORY = Path(os.environ.get("LOOP_HISTORY_ROOT", REPO / "data" / "local" / "history"))


def ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def k1s(sym, start, o, h, l, c, v=1.0, n=1, closed=True, q=None, V=None, recv_ns=0) -> Tick:
    """A parsed kline_1s tick, built through the real parser from a raw combined-stream message."""
    k = {"t": start, "T": start + 999, "s": sym, "i": "1s", "o": repr(o), "c": repr(c), "h": repr(h),
         "l": repr(l), "v": repr(v), "n": n, "x": closed,
         "q": repr(v * c if q is None else q), "V": repr(v / 2 if V is None else V)}
    raw = json.dumps({"stream": f"{sym.lower()}@kline_1s", "data": {"e": "kline", "E": start + SEC, "s": sym, "k": k}})
    return parse(raw, recv_ns)


def agg(sym, t, p, q, agg_id, first_id=None, last_id=None, maker=False) -> Tick:
    first_id = agg_id * 10 if first_id is None else first_id
    last_id = first_id if last_id is None else last_id
    raw = json.dumps({"stream": f"{sym.lower()}@aggTrade", "data": {
        "e": "aggTrade", "E": t, "s": sym, "a": agg_id, "p": repr(p), "q": repr(q), "f": first_id,
        "l": last_id, "T": t, "m": maker}})
    return parse(raw, 0)


def walk(sym: str, start: int, n: int, seed: int, p0: float = 100.0, flat_every: int = 0) -> list[B.Bar]:
    """A seeded random walk of grid-aligned 1m bars (a test fixture, not market data)."""
    rnd = random.Random(seed)
    out, p = [], p0
    for i in range(n):
        t = start + i * MIN
        if flat_every and i % flat_every == flat_every - 1:
            out.append(B.Bar(sym, "1m", t, t + MIN, p, p, p, p, 0.0, 0.0, 0, 0.0))    # zero-trade minute
            continue
        o = p
        c = round(o * (1 + rnd.gauss(0, 0.002)), 2)
        h = round(max(o, c) * (1 + abs(rnd.gauss(0, 0.001))), 2)
        l = round(min(o, c) * (1 - abs(rnd.gauss(0, 0.001))), 2)
        v = round(rnd.uniform(0.5, 5), 4)
        out.append(B.Bar(sym, "1m", t, t + MIN, o, h, l, c, v, round(v * c, 4), rnd.randint(4, 40), round(v / 3, 4)))
        p = c
    return out


def merged_ticks(streams: dict[str, list[B.Bar]]):
    """Derived 1s-kline ticks of every symbol, merged in exchange-time order."""
    gens = [ticks_from_bar_stream(bars) for bars in streams.values()]
    return heapq.merge(*gens, key=lambda t: (t.data["start_ms"], t.symbol))


def ticks_from_bar_stream(bars):
    for b in bars:
        yield from ticks_from_bar(b)


def core_fields(events):
    return [(e["payload"]["strategy"], e["payload"]["symbol"], e["payload"]["side"], e["payload"]["action"],
             e["ts_ms"], e["payload"]["ref_price"], e["payload"]["reason"], e["payload"]["interval"])
            for e in events]


class Cross1m:
    """Test-only 1m strategy that fires often, so the equivalence test exercises many fills."""
    intervals = ("1m",)

    def __init__(self, n: int = 5):
        self.name, self.params, self._n = "cross1m", {"n": n}, n

    def on_bar(self, bar, ctx):
        h = ctx.history(bar.symbol, "1m", self._n)
        if len(h) < self._n:
            return []
        sma = sum(b.close for b in h) / self._n
        held = ctx.position(bar.symbol) is not None
        if not held and bar.close > sma * 1.0005:
            return [Signal(self.name, bar.symbol, "open", ctx.now_ms, "close>sma*1.0005", bar.close, "1m")]
        if held and bar.close < sma:
            return [Signal(self.name, bar.symbol, "close", ctx.now_ms, "close<sma", bar.close, "1m")]
        return []


class Recording:
    name, params, intervals = "recording", {}, ("1m", "1h")

    def __init__(self):
        self.seen = []

    def on_bar(self, bar, ctx):
        h = ctx.history(bar.symbol, bar.interval, 10_000)
        self.seen.append((ctx.now_ms, bar.interval, bar.end_ms, max(b.end_ms for b in h)))
        return []


def account(mode="REPLAY", slots=2):
    return PaperAccount(1000, slots, CostModel(0.001, 2, 2), name="net", mode=mode)


# --- bar builder -----------------------------------------------------------------------

def test_builder_closes_a_bar_only_when_its_minute_has_ended():
    t0 = ms("2026-09-01T00:00:00")
    bb = BarBuilder("kline_1s")
    evs = []
    for s in range(59):
        evs += bb.feed(k1s("BTCUSDT", t0 + s * SEC, 100 + s, 101 + s, 99 + s, 100.5 + s, v=1.0, n=2))
    assert all(e[1] == "open" for e in evs), "no bar may close before its minute has ended"
    evs = bb.feed(k1s("BTCUSDT", t0 + 59 * SEC, 159, 160, 158, 159.5, v=1.0, n=2))
    (close,) = [e for e in evs if e[1] == "close"]
    bar = close[3]
    assert close[0] == t0 + MIN == bar.end_ms and bar.open_ms == t0 and bar.interval == "1m"
    assert (bar.open, bar.high, bar.low, bar.close) == (100, 160, 99, 159.5)
    assert bar.volume == 60.0 and bar.trades == 120


def test_builder_ignores_zero_trade_seconds_for_ohlc_but_keeps_a_flat_minute():
    t0 = ms("2026-09-01T00:00:00")
    bb = BarBuilder("kline_1s")
    evs = bb.feed(k1s("ETHUSDT", t0, 50, 50, 50, 50, v=0.0, n=0))           # carried price, no trade
    evs += bb.feed(k1s("ETHUSDT", t0 + 5 * SEC, 51, 52, 51, 52, v=2.0, n=3))
    evs += bb.feed(k1s("ETHUSDT", t0 + 59 * SEC, 52, 52, 52, 52, v=0.0, n=0, closed=True))
    (opened,) = [e for e in evs if e[1] == "open"]
    assert opened[0] == t0 and opened[3].open == 51                      # open = the first TRADE
    (bar,) = [e[3] for e in evs if e[1] == "close"]
    assert (bar.open, bar.high, bar.low, bar.close, bar.trades) == (51, 52, 51, 52, 3)
    # a minute with no trade at all is a flat bar at the carried price (Binance's own convention)
    evs = bb.feed(k1s("ETHUSDT", t0 + MIN, 52, 52, 52, 52, v=0.0, n=0))
    evs += bb.feed(k1s("ETHUSDT", t0 + MIN + 59 * SEC, 52, 52, 52, 52, v=0.0, n=0))
    kinds = [e[1] for e in evs]
    assert kinds == ["open", "close"]
    flat = evs[1][3]
    assert (flat.open, flat.high, flat.low, flat.close, flat.volume, flat.trades) == (52, 52, 52, 52, 0.0, 0)


def test_builder_drops_unclosed_late_and_duplicate_ticks_and_never_restates_a_closed_bar():
    t0 = ms("2026-09-01T00:00:00")
    bb = BarBuilder("kline_1s")
    assert bb.feed(k1s("BTCUSDT", t0, 1, 1, 1, 1, closed=False)) == []       # a still-forming second
    bb.feed(k1s("BTCUSDT", t0, 10, 10, 10, 10))
    assert bb.feed(k1s("BTCUSDT", t0, 99, 99, 99, 99)) == []                 # duplicate second
    (close,) = [e for e in bb.feed(k1s("BTCUSDT", t0 + MIN, 11, 11, 11, 11)) if e[1] == "close"]
    assert close[3].high == 10
    assert bb.feed(k1s("BTCUSDT", t0 + 30 * SEC, 500, 500, 500, 500)) == []  # late: that minute is closed
    assert bb.stats["unclosed"] == 1 and bb.stats["duplicate"] == 1 and bb.stats["late"] == 1
    assert close[3].close == 10


def test_builder_from_aggtrades_sums_quote_volume_taker_buys_and_trade_counts():
    t0 = ms("2026-09-01T00:00:00")
    bb = BarBuilder("aggTrade")
    evs = bb.feed(agg("BTCUSDT", t0 - 1, 10.0, 0.5, 0))           # listening since before the minute
    evs += bb.feed(agg("BTCUSDT", t0 + 100, 10.0, 2.0, 1, first_id=10, last_id=12))               # taker buy
    evs += bb.feed(agg("BTCUSDT", t0 + 30 * SEC, 12.0, 1.0, 2, first_id=13, last_id=13, maker=True))
    evs += bb.feed(agg("BTCUSDT", t0 + 59 * SEC, 9.0, 1.0, 3, first_id=14, last_id=15))
    assert [e[1] for e in evs] == ["open"]                     # a trade stream can't know a minute is over...
    evs = bb.feed(agg("BTCUSDT", t0 + MIN + 1, 9.5, 1.0, 4))   # ...until time moves past it
    bar = [e[3] for e in evs if e[1] == "close"][0]
    assert (bar.open, bar.high, bar.low, bar.close) == (10.0, 12.0, 9.0, 9.0)
    assert bar.volume == 4.0 and bar.quote_volume == 2 * 10.0 + 12.0 + 9.0
    assert bar.taker_buy_base == 3.0 and bar.trades == 6


def test_the_minute_in_progress_at_start_or_after_a_reconnect_is_never_emitted():
    """Found by the first live run: the scanner started mid-minute and closed a 20-second 'bar'."""
    t0 = ms("2026-09-01T00:00:00")
    bb, evs = BarBuilder("kline_1s"), []
    for s in range(40, 60):                                       # joined at :40
        evs += bb.feed(k1s("BTCUSDT", t0 + s * SEC, 1, 1, 1, 1))
    assert evs == [] and bb.stats["incomplete"] == 1
    for s in range(60):                                           # the next minute is seen whole
        evs += bb.feed(k1s("BTCUSDT", t0 + MIN + s * SEC, 2, 2, 2, 2))
    assert [e[1] for e in evs] == ["open", "close"] and evs[1][3].open_ms == t0 + MIN
    bb.feed(k1s("BTCUSDT", t0 + 2 * MIN, 3, 3, 3, 3))
    assert bb.discard_open() == 1                                 # reconnect: the hole is a gap
    evs = []
    for s in range(10, 60):
        evs += bb.feed(k1s("BTCUSDT", t0 + 3 * MIN + s * SEC, 4, 4, 4, 4))
    assert evs == [] and bb.stats["incomplete"] == 2


def test_a_late_open_still_fills_the_pending_paper_order_at_its_open_price():
    t0 = ms("2026-09-01T00:00:00")

    class OpenOnce:
        name, params, intervals = "open_once", {}, ("1m",)

        def __init__(self):
            self.done = False

        def on_bar(self, bar, ctx):
            if bar.symbol == "ETHUSDT" and not self.done:
                self.done = True
                return [Signal(self.name, "ETHUSDT", "open", ctx.now_ms, "t", bar.close, "1m")]
            return []

    acc = account("LIVE_PAPER")
    sc = Scanner(["BTCUSDT", "ETHUSDT"], [OpenOnce()], {"net": acc}, grace_ms=3000)
    for s in range(60):
        sc.on_tick(k1s("BTCUSDT", t0 + s * SEC, 1, 1, 1, 1))
        sc.on_tick(k1s("ETHUSDT", t0 + s * SEC, 5, 5, 5, 5))
    for s in range(10):                                           # ETH quiet (zero-trade seconds)...
        sc.on_tick(k1s("BTCUSDT", t0 + MIN + s * SEC, 1, 1, 1, 1))
        sc.on_tick(k1s("ETHUSDT", t0 + MIN + s * SEC, 5, 5, 5, 5, v=0.0, n=0))
    assert acc.positions == {}                                    # signal at t0+1m, no ETH trade yet
    sc.on_tick(k1s("ETHUSDT", t0 + MIN + 10 * SEC, 6, 6, 6, 6))   # ...first ETH trade, after the release
    assert sc.stats["late_opens_applied"] == 1
    assert acc.positions["ETHUSDT"]["entry_mid"] == 6 and acc.positions["ETHUSDT"]["opened_ms"] == t0 + MIN


def test_bars_built_from_derived_ticks_equal_the_source_bars():
    src = walk("BTCUSDT", ms("2025-06-01T00:00:00"), 600, seed=7, flat_every=97)
    bb, out = BarBuilder("kline_1s"), []
    for b in src:
        for t in ticks_from_bar(b):
            out += [e[3] for e in bb.feed(t) if e[1] == "close"]
    assert out == src                              # same bars, same times, every field


# --- the scanner loop: same instants, no partial bar, no look-ahead ---------------------

def test_no_partial_bar_ever_reaches_a_strategy():
    t0 = ms("2026-09-01T00:00:00")
    rec = Recording()
    sc = Scanner(["BTCUSDT"], [rec], {"net": account("LIVE_PAPER", 1)}, grace_ms=None)
    for s in range(30):
        sc.on_tick(k1s("BTCUSDT", t0 + s * SEC, 1, 1, 1, 1))
    assert rec.seen == []                           # half a minute: nothing closed, nothing seen
    for s in range(30, 60):
        sc.on_tick(k1s("BTCUSDT", t0 + s * SEC, 1, 1, 1, 1))
    sc.on_tick(k1s("BTCUSDT", t0 + MIN, 1, 1, 1, 1))
    assert [(iv, end) for _, iv, end, _ in rec.seen] == [("1m", t0 + MIN)]
    assert all(latest <= now and end <= now for now, _, end, latest in rec.seen)


def test_an_instant_waits_for_every_symbol_so_btc_is_visible_to_eth_at_the_same_time():
    t0 = ms("2026-09-01T00:00:00")
    seen = []

    class Peek:
        name, params, intervals = "peek", {}, ("1m",)

        def on_bar(self, bar, ctx):
            b = ctx.last("BTCUSDT", "1m")
            seen.append((bar.symbol, bar.end_ms, b.end_ms if b else None))
            return []

    sc = Scanner(["BTCUSDT", "ETHUSDT"], [Peek()], {"net": account("LIVE_PAPER")}, grace_ms=None)
    sc.on_tick(k1s("ETHUSDT", t0, 5, 5, 5, 5))
    sc.on_tick(k1s("BTCUSDT", t0, 1, 1, 1, 1))
    sc.on_tick(k1s("ETHUSDT", t0 + MIN, 5, 5, 5, 5))      # ETH's minute is over, BTC's not yet known
    assert seen == []
    sc.on_tick(k1s("BTCUSDT", t0 + MIN, 1, 1, 1, 1))
    assert sorted(seen) == [("BTCUSDT", t0 + MIN, t0 + MIN), ("ETHUSDT", t0 + MIN, t0 + MIN)]


def test_a_stalled_symbol_is_released_by_grace_and_its_late_bar_is_dropped_and_counted():
    t0 = ms("2026-09-01T00:00:00")
    sc = Scanner(["BTCUSDT", "ETHUSDT"], [Recording()], {"net": account("LIVE_PAPER")}, grace_ms=3000)
    sc.on_tick(k1s("BTCUSDT", t0, 1, 1, 1, 1))
    sc.on_tick(k1s("ETHUSDT", t0, 5, 5, 5, 5))
    for s in range(1, 10):                                 # ETH goes silent; BTC keeps ticking
        sc.on_tick(k1s("BTCUSDT", t0 + MIN + s * SEC, 1, 1, 1, 1))
    assert sc.stats["instants"] >= 1                       # the minute was not held hostage by ETH
    before = sc.stats["late_events"]
    sc.on_tick(k1s("ETHUSDT", t0 + 20 * SEC, 6, 6, 6, 6))   # ETH wakes up inside a closed minute
    assert sc.stats["late_events"] + sc.builder.stats["late"] > before
    assert all(e.symbol != "ETHUSDT" or e.close == 5 for e in sc.core.store.tail("ETHUSDT", "1m", 10))


# --- THE KEY TEST: live path == replay, signal for signal -------------------------------

def _both_paths(streams: dict[str, list[B.Bar]], make_strategies):
    a_rep, a_live = account("REPLAY"), account("REPLAY")
    rep = replay({s: iter(b) for s, b in streams.items()}, make_strategies(), {"net": a_rep})
    sc = Scanner(list(streams), make_strategies(), {"net": a_live}, mode="REPLAY", grace_ms=None, keep_bars=True)
    for t in merged_ticks(streams):
        sc.on_tick(t)
    sc.finish()
    assert a_rep.trades == a_live.trades and a_rep.positions == a_live.positions   # same paper book too
    return rep, sc


def test_KEY_live_path_emits_identical_signals_to_replay_on_the_same_history():
    d0 = ms("2025-01-01T00:00:00")
    streams = {"BTCUSDT": walk("BTCUSDT", d0, 3 * 1440, seed=1, p0=40_000, flat_every=211),
               "ETHUSDT": walk("ETHUSDT", d0, 3 * 1440, seed=2, p0=3_000)}
    rep, sc = _both_paths(streams, lambda: [TrendSMA(sma=2, btc_sma=2), Cross1m(5)])
    live = sc.core.res
    assert rep.signals > 50, "the fixture must actually fire, or equality proves nothing"
    assert core_fields(live.signal_events) == core_fields(rep.signal_events)
    assert [b for s in streams for b in sc.closed_bars[s]] == [b for s in streams for b in streams[s]]
    for s in streams:
        assert rep.store.all(s, "1d") == live.store.all(s, "1d")
    assert rep.fills == live.fills
    assert sc.stats["late_events"] == 0


def _history_available(*syms):
    return all(loader.month_files(HISTORY / "monthly", s) for s in syms)


@pytest.mark.skipif(not _history_available("BTCUSDT", "ETHUSDT"),
                    reason="needs data/local/history/monthly (T2.6); set LOOP_HISTORY_ROOT")
def test_KEY_live_path_equals_replay_on_recorded_binance_history():
    """Real BTC/ETH 1m history (2025-09 .. 2026-02, before the holdout), as ticks vs as bars."""
    t0, t1 = windows.ym_ms("2025-09"), windows.ym_ms("2026-03")
    root = HISTORY / "monthly"
    streams = {s: list(loader.iter_symbol(root, s, start_ms=t0, end_ms=t1)) for s in ("BTCUSDT", "ETHUSDT")}
    assert all(b.end_ms - b.open_ms == MIN for bars in streams.values() for b in bars)
    rep, sc = _both_paths(streams, lambda: [TrendSMA(sma=50, btc_sma=50), Cross1m(30)])
    print(f"\nrecorded-history equivalence: bars={sum(map(len, streams.values()))} "
          f"replay signals={rep.signals} live signals={sc.core.res.signals} ticks={sc.stats['ticks']} "
          f"trend_sma signals={sum(e['payload']['strategy'] == 'trend_sma' for e in rep.signal_events)}")
    assert rep.signals > 0
    assert core_fields(sc.core.res.signal_events) == core_fields(rep.signal_events)
    trend = [e for e in rep.signal_events if e["payload"]["strategy"] == "trend_sma"]
    assert trend, "the 50-day trend rule must fire at least once in this window"
    assert rep.fills == sc.core.res.fills and sc.stats["late_events"] == 0


@pytest.mark.skipif(not (HISTORY / "klines_1s" / "BTCUSDT").is_dir(), reason="needs data/local/history/klines_1s")
def test_real_binance_1s_klines_build_the_same_bars_as_binance_1m_klines():
    day = sorted((HISTORY / "klines_1s" / "BTCUSDT").glob("BTCUSDT-1s-*.zip"))[0]
    ym = day.name[len("BTCUSDT-1s-"):len("BTCUSDT-1s-") + 7]
    m1 = HISTORY / "monthly" / "klines_1m" / "BTCUSDT" / f"BTCUSDT-1m-{ym}.zip"
    if not m1.exists():
        pytest.skip(f"no 1m month file {m1.name}")
    d0 = ms(day.name[len("BTCUSDT-1s-"):-4] + "T00:00:00")
    want = [b for b in loader.iter_klines(m1, "BTCUSDT") if d0 <= b.open_ms < d0 + DAY]
    bb, got = BarBuilder("kline_1s"), []
    for s in loader.iter_klines(day, "BTCUSDT", "1s"):
        t = Tick("kline", "BTCUSDT", s.end_ms, 0, {
            "interval": "1s", "start_ms": s.open_ms, "open": s.open, "high": s.high, "low": s.low,
            "close": s.close, "volume": s.volume, "trades": s.trades, "closed": True,
            "quote_volume": s.quote_volume, "taker_buy_base": s.taker_buy_base})
        got += [e[3] for e in bb.feed(t) if e[1] == "close"]
    want = want[:len(got)]                       # the day's last minute closes only when time moves on
    print(f"\nreal 1s->1m: {day.name}: built {len(got)} bars, compared to {len(want)} Binance 1m bars")
    assert len(got) >= 1439
    assert [(b.open_ms, b.end_ms, b.open, b.high, b.low, b.close, b.trades) for b in got] == \
           [(b.open_ms, b.end_ms, b.open, b.high, b.low, b.close, b.trades) for b in want]
    assert all(abs(g.volume - w.volume) <= 1e-6 * max(1.0, w.volume) for g, w in zip(got, want))


# --- signal records: LIVE_PAPER, simulated, append-only ----------------------------------

def _firing_scanner(tmp_path, mode="LIVE_PAPER"):
    d0 = ms("2025-01-01T00:00:00")
    streams = {"BTCUSDT": walk("BTCUSDT", d0, 240, seed=3), "ETHUSDT": walk("ETHUSDT", d0, 240, seed=4)}
    sink = DailySink(tmp_path)
    sc = Scanner(list(streams), [Cross1m(5)], {"net": account(mode)}, mode=mode, sink=sink, grace_ms=None)
    recs = []
    for t in merged_ticks(streams):
        recs += sc.on_tick(t)
    recs += sc.finish()
    sink.close()
    return recs


def test_signal_records_are_live_paper_simulated_t61_envelopes(tmp_path):
    recs = _firing_scanner(tmp_path)
    assert recs
    files = sorted(tmp_path.glob("*.jsonl"))
    assert [f.name for f in files] == ["2025-01-01.jsonl"]
    lines = [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()]
    assert lines == recs
    for r in lines:
        assert r["v"] == 1 and r["type"] == "signal" and r["mode"] == "LIVE_PAPER" and r["simulated"] is True
        assert r["run_id"] == "live-2025-01-01"
        d = r["data"]
        assert d["side"] == "LONG" and d["strategy_id"] == "cross1m" and d["signal_id"]
        assert d["class"] is None and d["cost_rt_pct"] is None and d["p_star"] is None   # UNKNOWN, not 0
        assert r["ts_event_ms"] == int(d["signal_id"].rsplit(":", 1)[1])
    seqs = [r["seq"] for r in lines]
    assert seqs == list(range(1, len(seqs) + 1))


def test_signal_file_is_append_only_and_seq_continues_after_a_restart(tmp_path):
    first = _firing_scanner(tmp_path)
    second = _firing_scanner(tmp_path)
    lines = (tmp_path / "2025-01-01.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(first) + len(second)
    assert [json.loads(x)["seq"] for x in lines] == list(range(1, len(lines) + 1))


def test_scanner_refuses_an_unknown_mode():
    with pytest.raises(ValueError):
        Scanner(["BTCUSDT"], [Cross1m()], mode="LIVE")


# --- registry ------------------------------------------------------------------------------

def test_registry_lists_plugins_with_family_params_and_evidence_pointer():
    items = {p.name: p for p in R.DEFAULT.list()}
    t = items["trend_sma"]
    assert t.family == "trend" and t.params == {"sma": 50, "btc_sma": 50, "btc": "BTCUSDT"}
    assert t.gate_status == "PENDING"
    assert (REPO / t.evidence.split("#")[0]).exists(), "evidence must point at a file in the repo"
    s = R.DEFAULT.build("trend_sma", sma=20)
    assert s.name == "trend_sma" and s.params["sma"] == 20 and s.intervals == ("1d",)


def test_registry_refuses_duplicates_and_non_strategies():
    reg = R.Registry()
    reg.register(R.PluginInfo("cross1m", "test", Cross1m, {"n": 5}, evidence="UNKNOWN"))
    with pytest.raises(ValueError):
        reg.register(R.PluginInfo("cross1m", "test", Cross1m, {"n": 5}, evidence="UNKNOWN"))
    with pytest.raises(TypeError):
        reg.register(R.PluginInfo("nope", "test", dict, {}, evidence="UNKNOWN"))
    with pytest.raises(KeyError):
        reg.build("missing")


# --- CLI ----------------------------------------------------------------------------------

def test_cli_without_an_explicit_mode_refuses_to_run(capsys):
    from trading.scanner import run
    with pytest.raises(SystemExit) as e:
        run.main([])
    assert e.value.code != 0


def test_cli_once_replay_on_a_recorded_tick_file_writes_replay_records_not_live_files(tmp_path):
    from trading.scanner import run
    d0 = ms("2025-01-01T00:00:00")
    streams = {"BTCUSDT": walk("BTCUSDT", d0, 3 * 1440, seed=5), "ETHUSDT": walk("ETHUSDT", d0, 3 * 1440, seed=6)}
    rec = tmp_path / "ticks" / "kline" / "00.jsonl.gz"
    rec.parent.mkdir(parents=True)
    with gzip.open(rec, "wt", encoding="utf-8") as f:
        for t in merged_ticks(streams):
            f.write(json.dumps(t.to_record()) + "\n")
    out = tmp_path / "signals"
    summary = run.main(["--once-replay", str(rec), "--symbols", "BTCUSDT,ETHUSDT", "--strategies", "trend_sma",
                        "--param", "trend_sma.sma=2", "--param", "trend_sma.btc_sma=2", "--out", str(out)])
    assert summary["mode"] == "REPLAY" and summary["bars_closed"] == 2 * 3 * 1440 and summary["late_events"] == 0
    assert not list(out.glob("*.jsonl")), "offline runs never write into the dated LIVE_PAPER files"
    (f,) = list((out / "once-replay").glob("*.jsonl"))
    lines = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == summary["signals"] and all(r["mode"] == "REPLAY" and r["simulated"] for r in lines)


# --- safety -----------------------------------------------------------------------------------

def test_scanner_package_never_imports_an_exchange_client():
    code = (
        "import sys, trading.scanner.run, trading.scanner.scanner, trading.scanner.bars, trading.scanner.registry;"
        "bad=[m for m in sys.modules if m=='ccxt' or m.startswith('ccxt.') or m.startswith('trading.exchange')"
        " or m in ('trend_follower','live_executor','futures_executor','lev_satellite_manage')];"
        "print(','.join(bad))"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
    assert out.stdout.strip() == ""
