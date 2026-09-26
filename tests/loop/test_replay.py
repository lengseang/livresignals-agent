"""trading.replay: bar-by-bar historical replay with a hard no-look-ahead guarantee.

Design: docs/loop/designs/T3.0-T3.1.md. Every number here is REPLAY (simulated).
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading.replay import bars as B
from trading.replay import loader, windows
from trading.replay.broker import CostModel, PaperAccount
from trading.replay.context import BarStore, ClockError, Context, LookAheadError, ReplayClock
from trading.replay.engine import replay
from trading.replay.plugins.trend_sma import TrendSMA
from trading.replay.resample import Resampler
from trading.replay.strategy import Signal

MIN = 60_000
DAY = 86_400_000


def ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def row(open_ms: int, o, h, l, c, v=1.0, micro=False) -> str:
    k = 1000 if micro else 1
    return (f"{open_ms * k},{o},{h},{l},{c},{v},{(open_ms + MIN) * k - 1},{v * c},3,{v / 2},{v * c / 2},0")


def write_zip(path: Path, lines: list[str], header: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ("open_time,open,high,low,close,volume,close_time,quote_volume,count,"
            "taker_buy_volume,taker_buy_quote_volume,ignore\n" if header else "") + "\n".join(lines) + "\n"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(path.name.replace(".zip", ".csv"), body)
    return path


def bar1m(sym: str, open_ms: int, o, h=None, l=None, c=None, v=1.0) -> B.Bar:
    c = o if c is None else c
    return B.Bar(sym, "1m", open_ms, open_ms + MIN, o, h if h is not None else max(o, c),
                 l if l is not None else min(o, c), c, v, v * c, 1, v / 2)


def daily_minutes(sym: str, day0_ms: int, closes: list[float], per_day: int = 3) -> list[B.Bar]:
    """A few 1m bars per UTC day, the last one at 23:59 so the day closes on time."""
    out = []
    for d, c in enumerate(closes):
        base = day0_ms + d * DAY
        for i in range(per_day - 1):
            out.append(bar1m(sym, base + i * MIN, c))
        out.append(bar1m(sym, base + DAY - MIN, c))
    return out


# --- timestamps and the loader -------------------------------------------------

def test_to_ms_detects_milliseconds_and_microseconds_by_magnitude():
    assert B.to_ms(1733011200000) == 1733011200000            # 2024-12 file: ms
    assert B.to_ms(1735689600000000) == 1735689600000          # 2025-01 file: µs
    assert B.to_ms(1735689659999999) == 1735689659999
    with pytest.raises(ValueError):
        B.to_ms(1735689600)                                     # seconds: refuse rather than guess


def test_loader_normalises_ms_and_us_files_to_identical_bars(tmp_path):
    t = ms("2025-01-01T00:00:00")
    a = write_zip(tmp_path / "a" / "BTCUSDT-1m-2025-01.zip", [row(t, 1, 2, 0.5, 1.5), row(t + MIN, 1.5, 2, 1, 2)])
    b = write_zip(tmp_path / "b" / "BTCUSDT-1m-2025-01.zip",
                  [row(t, 1, 2, 0.5, 1.5, micro=True), row(t + MIN, 1.5, 2, 1, 2, micro=True)])
    ra, rb = list(loader.iter_klines(a, "BTCUSDT")), list(loader.iter_klines(b, "BTCUSDT"))
    assert ra == rb
    assert ra[0].open_ms == t and ra[0].end_ms == t + MIN and ra[1].close == 2.0


def test_loader_skips_a_header_row(tmp_path):
    t = ms("2024-12-01T00:00:00")
    z = write_zip(tmp_path / "BTCUSDT-1m-2024-12.zip", [row(t, 1, 1, 1, 1)], header=True)
    assert [x.open_ms for x in loader.iter_klines(z, "BTCUSDT")] == [t]


def test_loader_is_lazy(tmp_path):
    t = ms("2024-12-01T00:00:00")
    z = write_zip(tmp_path / "BTCUSDT-1m-2024-12.zip", [row(t + i * MIN, 1, 1, 1, 1) for i in range(5)])
    it = loader.iter_klines(z, "BTCUSDT")
    assert next(it).open_ms == t                                 # a generator, not a list


def test_iter_symbol_spans_the_ms_to_us_switch_in_order_and_clips_the_window(tmp_path):
    root = tmp_path / "monthly"
    d = root / "klines_1m" / "BTCUSDT"
    t1, t2 = ms("2024-12-31T23:58:00"), ms("2025-01-01T00:00:00")
    write_zip(d / "BTCUSDT-1m-2024-12.zip", [row(t1, 1, 1, 1, 1), row(t1 + MIN, 2, 2, 2, 2)])
    write_zip(d / "BTCUSDT-1m-2025-01.zip", [row(t2, 3, 3, 3, 3, micro=True), row(t2 + MIN, 4, 4, 4, 4, micro=True)])
    got = [x.close for x in loader.iter_symbol(root, "BTCUSDT")]
    assert got == [1, 2, 3, 4]
    got = [x.close for x in loader.iter_symbol(root, "BTCUSDT", start_ms=t1 + MIN, end_ms=t2 + MIN)]
    assert got == [2, 3]


def test_iter_symbol_refuses_out_of_order_rows(tmp_path):
    root = tmp_path / "monthly"
    t = ms("2024-12-01T00:00:00")
    write_zip(root / "klines_1m" / "BTCUSDT" / "BTCUSDT-1m-2024-12.zip", [row(t + MIN, 1, 1, 1, 1), row(t, 1, 1, 1, 1)])
    with pytest.raises(ValueError):
        list(loader.iter_symbol(root, "BTCUSDT"))


def test_loader_keeps_real_off_grid_timestamps(tmp_path):
    """Late-2017 history is phase-shifted off the minute grid (measured: BTCUSDT-1m-2017-12)."""
    lines = ["1512367200000,1,1,1,1,1,1512367220798,1,1,0.5,0.5,0",          # a short 20.8 s bar
             "1512367220799,1,2,1,2,1,1512367280798,2,1,0.5,1,0"]             # a shifted 1m bar
    z = write_zip(tmp_path / "BTCUSDT-1m-2017-12.zip", lines)
    a, b = loader.iter_klines(z, "BTCUSDT")
    assert (a.open_ms, a.end_ms) == (1512367200000, 1512367220799)
    assert (b.open_ms, b.end_ms) == (1512367220799, 1512367280799)


def test_loader_rejects_a_close_time_outside_the_bar(tmp_path):
    z = write_zip(tmp_path / "BTCUSDT-1m-2017-12.zip", ["1512367200000,1,1,1,1,1,1512367400000,1,1,0.5,0.5,0"])
    with pytest.raises(ValueError):
        list(loader.iter_klines(z, "BTCUSDT"))
    z = write_zip(tmp_path / "x" / "BTCUSDT-1m-2020-12.zip", ["1608559740000,1,1,1,1,1,1608558440521,1,3,0.5,0.5,0"])
    with pytest.raises(ValueError):                              # close before open WITH trades: a real defect
        list(loader.iter_klines(z, "BTCUSDT"))


def test_loader_drops_and_counts_a_zero_trade_placeholder_with_close_before_open(tmp_path):
    """Measured: BTCUSDT-1m-2020-12 has open 1608559740000, close 1608558440521, 0 volume, 0 trades."""
    t = 1608559680000
    z = write_zip(tmp_path / "BTCUSDT-1m-2020-12.zip", [
        row(t, 5, 5, 5, 5), "1608559740000,22646.53,22646.53,22646.53,22646.53,0,1608558440521,0,0,0,0,0",
        row(t + 2 * MIN, 6, 6, 6, 6)])
    stats = {}
    got = list(loader.iter_klines(z, "BTCUSDT", stats=stats))
    assert [b.close for b in got] == [5, 6] and stats == {"BTCUSDT:dropped_placeholder": 1}


# --- resampling ------------------------------------------------------------------

def test_resampler_aggregates_a_day_and_emits_only_when_it_has_ended():
    d0 = ms("2021-01-01T00:00:00")
    r = Resampler("BTCUSDT", "1d")
    out = r.add(bar1m("BTCUSDT", d0, 10, 12, 9, 11, v=2))
    out += r.add(bar1m("BTCUSDT", d0 + MIN, 11, 15, 8, 14, v=3))
    assert out == [] and r.flush_until(d0 + 2 * MIN) is None   # day not over: nothing leaks
    r.add(bar1m("BTCUSDT", d0 + DAY - MIN, 14, 14, 13, 13, v=1))
    day = r.flush_until(d0 + DAY)
    assert (day.open, day.high, day.low, day.close, day.volume) == (10, 15, 8, 13, 6)
    assert day.open_ms == d0 and day.end_ms == d0 + DAY and day.interval == "1d"


def test_resampler_with_a_missing_last_minute_never_includes_next_day_data():
    d0 = ms("2021-01-01T00:00:00")
    r = Resampler("BTCUSDT", "1d")
    r.add(bar1m("BTCUSDT", d0, 10))
    assert r.flush_until(d0 + DAY - 1) is None
    out = r.add(bar1m("BTCUSDT", d0 + DAY + 5 * MIN, 99))      # 23:59 missing; next day's bar arrives
    assert len(out) == 1 and out[0].close == 10 and out[0].end_ms == d0 + DAY


def test_a_bar_straddling_midnight_belongs_to_the_day_it_closes_in():
    d1 = ms("2021-01-02T00:00:00")
    r = Resampler("BTCUSDT", "1d")
    r.add(B.Bar("BTCUSDT", "1m", d1 - 2 * MIN + 20_000, d1 - MIN + 20_000, 1, 1, 1, 1, 1, 1, 1, 1))
    out = r.add(B.Bar("BTCUSDT", "1m", d1 - MIN + 20_000, d1 + 20_000, 7, 7, 7, 7, 1, 1, 1, 1))
    (day1,) = out                                               # day 1 ends with the bar that closed in it
    assert r.flush_until(d1) is None                            # day 2 is still open at midnight
    assert day1.close == 1 and day1.end_ms == d1                # the straddling bar is not in day 1
    assert r.flush_until(d1 + DAY).open == 7                    # it opens day 2


def test_phase_shifted_symbols_replay_on_their_real_clock():
    """BTC and ETH shifted by different offsets (as in 2017 history): no clock error, fills at real opens."""
    d0 = ms("2021-01-01T00:00:00")

    def shifted(sym, off, closes):
        out = []
        for d, c in enumerate(closes):
            for i in (-2, -1):
                o = d0 + (d + 1) * DAY + i * MIN + off
                out.append(B.Bar(sym, "1m", o, o + MIN, c, c, c, c, 1, c, 1, 0.5))
        return out

    acc = PaperAccount(1000, 2, CostModel(0, 0, 0))
    streams = {"BTCUSDT": shifted("BTCUSDT", 20_798, [1, 2, 3, 4]), "ETHUSDT": shifted("ETHUSDT", 20_809, [1, 2, 3, 4])}
    rec = Recorder()
    replay(streams, [rec, OpenThenClose()], {"g": acc})
    assert all(latest <= now for now, latest, _ in rec.seen)
    assert all(t["opened_ms"] >= t["signal_ms"] for t in acc.trades)


# --- clock and context: the look-ahead guard ---------------------------------------

def test_clock_is_monotonic():
    c = ReplayClock()
    c.advance(10)
    c.advance(10)
    with pytest.raises(ClockError):
        c.advance(9)


def _ctx_with_days(n_days: int, now_ms: int):
    d0 = ms("2021-01-01T00:00:00")
    store, clock = BarStore(), ReplayClock()
    clock.advance(now_ms)
    for i in range(n_days):
        store.append(B.Bar("BTCUSDT", "1d", d0 + i * DAY, d0 + (i + 1) * DAY, 1, 1, 1, float(i), 1, 1, 1, 1))
    return Context(store, clock, PaperAccount(1000, 1, CostModel())), d0


def test_context_history_returns_only_closed_bars():
    d0 = ms("2021-01-01T00:00:00")
    ctx, _ = _ctx_with_days(3, d0 + 3 * DAY)
    h = ctx.history("BTCUSDT", "1d", 10)
    assert [b.close for b in h] == [0, 1, 2] and all(b.end_ms <= ctx.now_ms for b in h)


def test_canary_bar_at_a_future_time_raises():
    d0 = ms("2021-01-01T00:00:00")
    ctx, _ = _ctx_with_days(3, d0 + 2 * DAY)
    assert ctx.bar_at("BTCUSDT", "1d", d0 + 2 * DAY).close == 1
    with pytest.raises(LookAheadError):
        ctx.bar_at("BTCUSDT", "1d", d0 + 3 * DAY)


def test_canary_a_store_that_holds_a_future_bar_is_caught_on_read():
    d0 = ms("2021-01-01T00:00:00")
    ctx, _ = _ctx_with_days(3, d0 + 2 * DAY)     # the 3rd bar ends after now: a leak in the store
    with pytest.raises(LookAheadError):
        ctx.history("BTCUSDT", "1d", 5)
    with pytest.raises(LookAheadError):
        ctx.last("BTCUSDT", "1d")


class Cheater:
    """Tries to read tomorrow's daily bar. The harness must refuse."""
    name, params, intervals = "cheater", {}, ("1d",)

    def on_bar(self, bar, ctx):
        ctx.bar_at(bar.symbol, "1d", bar.end_ms + DAY)
        return []


class Recorder:
    name, params, intervals = "recorder", {}, ("1d",)

    def __init__(self):
        self.seen = []

    def on_bar(self, bar, ctx):
        h = ctx.history(bar.symbol, "1d", 1000)
        self.seen.append((ctx.now_ms, max(b.end_ms for b in h), bar.end_ms))
        return []


def test_canary_full_replay_with_a_cheating_strategy_raises():
    d0 = ms("2021-01-01T00:00:00")
    streams = {"BTCUSDT": daily_minutes("BTCUSDT", d0, [1, 2, 3])}
    with pytest.raises(LookAheadError):
        replay(streams, [Cheater()], {"net": PaperAccount(1000, 1, CostModel())})


def test_recording_strategy_never_observes_the_future():
    d0 = ms("2021-01-01T00:00:00")
    streams = {s: daily_minutes(s, d0, [1, 2, 3, 4]) for s in ("BTCUSDT", "ETHUSDT")}
    rec = Recorder()
    replay(streams, [rec], {"net": PaperAccount(1000, 1, CostModel())})
    assert len(rec.seen) == 8
    assert all(latest <= now and end <= now for now, latest, end in rec.seen)


def test_same_instant_fairness_btc_daily_is_visible_to_eth_on_the_same_day():
    d0 = ms("2021-01-01T00:00:00")
    streams = {"ETHUSDT": daily_minutes("ETHUSDT", d0, [5, 6]), "BTCUSDT": daily_minutes("BTCUSDT", d0, [1, 2])}
    seen = []

    class Peek:
        name, params, intervals = "peek", {}, ("1d",)

        def on_bar(self, bar, ctx):
            b = ctx.last("BTCUSDT", "1d")
            seen.append((bar.symbol, bar.end_ms, b.end_ms if b else None))
            return []

    replay(streams, [Peek()], {"net": PaperAccount(1000, 1, CostModel())})
    assert all(sym_end == btc_end for _, sym_end, btc_end in seen)


# --- execution and the shared account ---------------------------------------------

class OpenThenClose:
    """Opens on day 1's close, closes on day 2's close."""
    name, params, intervals = "oc", {}, ("1d",)

    def __init__(self):
        self.n = 0

    def on_bar(self, bar, ctx):
        self.n += 1
        if self.n == 1:
            return [Signal("oc", bar.symbol, "open", ctx.now_ms, "t", bar.close, "1d")]
        if self.n == 2:
            return [Signal("oc", bar.symbol, "close", ctx.now_ms, "t", bar.close, "1d")]
        return []


def _two_day_stream():
    d0 = ms("2021-01-01T00:00:00")
    # day closes at 100, then 110; next-minute opens differ from closes so fill prices are traceable
    bars = [bar1m("BTCUSDT", d0 + DAY - MIN, 100, c=100),
            bar1m("BTCUSDT", d0 + DAY, 101, c=105),                  # first bar after signal 1: fill at 101
            bar1m("BTCUSDT", d0 + 2 * DAY - MIN, 110, c=110),
            bar1m("BTCUSDT", d0 + 2 * DAY, 111, c=111),              # fill for the close at 111
            bar1m("BTCUSDT", d0 + 3 * DAY - MIN, 111, c=111)]
    return {"BTCUSDT": bars}


def test_fills_at_next_1m_open_and_gross_equals_price_move_when_costs_are_zero():
    acc = PaperAccount(1000, 1, CostModel(0, 0, 0))
    res = replay(_two_day_stream(), [OpenThenClose()], {"gross": acc})
    (t,) = acc.trades
    assert t["entry"] == 101 and t["exit"] == 111
    assert t["opened_ms"] >= t["signal_ms"]
    assert acc.cash == pytest.approx(1000 * 111 / 101)
    assert res.fills["gross"] == 2


def test_fees_spread_and_slippage_are_charged_on_each_side():
    cm = CostModel(fee_rate=0.001, spread_bps=2, slippage_bps=2)
    acc = PaperAccount(1000, 1, cm)
    replay(_two_day_stream(), [OpenThenClose()], {"net": acc})
    (t,) = acc.trades
    buy, sell = 101 * (1 + 3e-4), 111 * (1 - 3e-4)
    assert t["entry"] == pytest.approx(buy) and t["exit"] == pytest.approx(sell)
    qty = 1000 / (buy * 1.001)
    assert acc.cash == pytest.approx(qty * sell * 0.999)
    assert t["fee"] == pytest.approx(qty * buy * 0.001 + qty * sell * 0.001)
    assert t["net_ret"] < t["gross_ret"]


def test_a_fill_priced_before_its_signal_is_refused():
    acc = PaperAccount(1000, 1, CostModel())
    acc.submit(Signal("x", "BTCUSDT", "open", 10 * MIN, "t", 1.0, "1d"))
    with pytest.raises(LookAheadError):
        acc.on_open(bar1m("BTCUSDT", 9 * MIN, 1.0))


def test_shared_account_splits_equity_across_slots_and_never_overspends():
    acc = PaperAccount(1000, 2, CostModel(0, 0, 0))
    acc.submit(Signal("x", "BTCUSDT", "open", 0, "t", 1, "1d"))
    acc.submit(Signal("x", "ETHUSDT", "open", 0, "t", 1, "1d"))
    acc.submit(Signal("x", "SOLUSDT", "open", 0, "t", 1, "1d"))
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        acc.on_open(bar1m(s, 0, 10.0))
    assert set(acc.positions) == {"BTCUSDT", "ETHUSDT"}          # 2 slots: the third is skipped
    assert acc.cash == pytest.approx(0) and acc.cash >= -1e-9
    assert any(e["type"] == "skip" for e in acc.events)


def test_equity_is_marked_every_minute_and_drawdown_is_intraday():
    d0 = ms("2021-01-01T00:00:00")
    bars = [bar1m("BTCUSDT", d0 + DAY - MIN, 100),
            bar1m("BTCUSDT", d0 + DAY, 100, l=50, c=60),          # intraminute crash, then partial recovery
            bar1m("BTCUSDT", d0 + DAY + MIN, 60, c=100),
            bar1m("BTCUSDT", d0 + 2 * DAY - MIN, 100)]

    class Hold:
        name, params, intervals = "hold", {}, ("1d",)

        def on_bar(self, bar, ctx):
            return [] if ctx.position(bar.symbol) else [Signal("hold", bar.symbol, "open", ctx.now_ms, "t", bar.close, "1d")]

    acc = PaperAccount(1000, 1, CostModel(0, 0, 0))
    replay({"BTCUSDT": bars}, [Hold()], {"gross": acc})
    assert acc.max_drawdown() == pytest.approx(0.4)             # marked at the 60 close


def test_signals_before_trade_start_are_not_traded():
    acc = PaperAccount(1000, 1, CostModel(0, 0, 0))
    streams = _two_day_stream()
    res = replay(streams, [OpenThenClose()], {"gross": acc}, trade_start_ms=ms("2021-01-05T00:00:00"))
    assert acc.trades == [] and res.fills["gross"] == 0 and res.warmup_signals == 2


# --- the trend plugin (read-only re-implementation of the trend_follower rule) ---------------

def _trend_streams(btc: list[float], eth: list[float]):
    d0 = ms("2021-01-01T00:00:00")
    return {"BTCUSDT": daily_minutes("BTCUSDT", d0, btc, per_day=1),
            "ETHUSDT": daily_minutes("ETHUSDT", d0, eth, per_day=1)}


def test_trend_plugin_warmup_emits_nothing():
    strat = TrendSMA(sma=5, btc_sma=5)
    acc = PaperAccount(1000, 2, CostModel(0, 0, 0))
    res = replay(_trend_streams([1, 2, 3, 4], [1, 2, 3, 4]), [strat], {"g": acc})
    assert res.signals == 0


def test_trend_plugin_needs_both_coin_and_btc_above_sma_and_exits_on_coin_break():
    btc_down = [10, 10, 10, 10, 10, 9, 8, 7, 6, 5]
    eth_up = [1, 1, 1, 1, 1, 2, 3, 4, 5, 6]
    acc = PaperAccount(1000, 2, CostModel(0, 0, 0))
    res = replay(_trend_streams(btc_down, eth_up), [TrendSMA(sma=5, btc_sma=5)], {"g": acc})
    assert res.signals == 0                                   # ETH trending but BTC filter is off

    btc_up = [1, 1, 1, 1, 1, 2, 3, 4, 5, 6, 7, 8]
    eth = [1, 1, 1, 1, 1, 2, 3, 4, 1, 1, 1, 1]                 # up, then breaks below its SMA
    acc = PaperAccount(1000, 2, CostModel(0, 0, 0))
    replay(_trend_streams(btc_up, eth), [TrendSMA(sma=5, btc_sma=5)], {"g": acc})
    kinds = [(e["type"], e["payload"]["symbol"]) for e in acc.events if e["type"] in ("signal", "close")]
    assert ("signal", "ETHUSDT") in kinds and ("close", "ETHUSDT") in kinds
    assert ("signal", "BTCUSDT") in kinds and ("close", "BTCUSDT") not in kinds


def test_every_event_is_labelled_replay():
    acc = PaperAccount(1000, 1, CostModel())
    replay(_two_day_stream(), [OpenThenClose()], {"net": acc})
    assert acc.events and all(e["mode"] == "REPLAY" for e in acc.events)


# --- windows ------------------------------------------------------------------------

def test_walk_forward_windows_tile_the_range():
    w = windows.walk_forward("2018-01", "2022-01", fit_months=24, trade_months=12)
    assert [(x.fit_start, x.trade_start, x.trade_end) for x in w] == [
        ("2018-01", "2020-01", "2021-01"), ("2019-01", "2021-01", "2022-01")]


def test_holdout_is_the_last_six_complete_months_and_is_refused_unless_opened():
    assert windows.holdout_start("2026-08") == "2026-03"
    windows.check_window(ms("2025-01-01T00:00:00"), ms("2026-03-01T00:00:00"), "2026-03", open_holdout=False)
    with pytest.raises(windows.HoldoutError):
        windows.check_window(ms("2025-01-01T00:00:00"), ms("2026-03-01T00:01:00"), "2026-03", open_holdout=False)
    windows.check_window(ms("2025-01-01T00:00:00"), ms("2026-09-01T00:00:00"), "2026-03", open_holdout=True)


# --- safety -----------------------------------------------------------------------

def test_replay_package_never_imports_an_exchange_client():
    code = (
        "import sys, trading.replay.run, trading.replay.engine, trading.replay.plugins.trend_sma;"
        "bad=[m for m in sys.modules if m=='ccxt' or m.startswith('ccxt.') or m.startswith('trading.exchange')"
        " or m in ('trend_follower','live_executor')];"
        "print(','.join(bad))"
    )
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
    assert out.stdout.strip() == ""
