"""T4.2: the paper execution engine and its measured fill model (docs/loop/designs/T4.1.md).

Paper only. Every test maps to one item of the T4.1 test plan (numbers in the comments), plus the kline
broker's wiring into the replay engine and the versioned spread table.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading.paper import fills as F
from trading.paper import spreads as S
from trading.paper.broker import KlineBroker
from trading.replay import bars as B
from trading.replay.broker import CostModel, PaperAccount
from trading.replay.engine import replay
from trading.replay.strategy import Signal
from trading.stream.normalize import Tick, parse

SEC = 1_000_000_000          # ns
MIN = 60_000                 # ms
DAY = 86_400_000
T0 = 1_790_000_000 * SEC     # an arbitrary receive-clock origin (ns)
FIXTURE = Path(__file__).parent / "fixtures" / "stream_sample.jsonl"


def book(t_s: float, bid: float, ask: float, bid_qty: float = 10.0, ask_qty: float = 10.0,
         sym: str = "BTCUSDT") -> Tick:
    return Tick("book", sym, None, T0 + int(t_s * SEC),
                {"bid": bid, "bid_qty": bid_qty, "ask": ask, "ask_qty": ask_qty, "update_id": 0})


def trade(t_s: float, price: float, qty: float = 1.0, sym: str = "BTCUSDT") -> Tick:
    return Tick("trade", sym, None, T0 + int(t_s * SEC),
                {"price": price, "qty": qty, "agg_id": 0, "first_id": 0, "last_id": 0,
                 "buyer_is_maker": False, "trade_ms": 0})


def at(t_s: float) -> int:
    return T0 + int(t_s * SEC)


# --- the versioned spread table --------------------------------------------------------------

def test_spread_table_is_the_measured_one_with_its_source_and_window():
    tab = S.load_table()
    assert "spreads.md" in tab["_source"] and "7.56" in tab["_source"]
    assert "2026-09-25" in tab["_window"] and "2026-09-26" in tab["_window"]
    assert S.spread_bp("BTCUSDT") == 0.0012
    assert S.spread_bp("DOTUSDT") == 8.3717
    assert S.spread_bp("ARBUSDT", "p90") == 8.8261
    assert S.spread_bp("ATOMUSDT", "p99") == 11.3379
    assert len(tab["symbols"]) == 15
    for row in tab["symbols"].values():
        assert row["ticks"] is None                 # tick size not recorded: UNKNOWN, not invented


def test_an_unmeasured_symbol_is_unknown_and_refused_unless_overridden():
    with pytest.raises(S.UnknownSpreadError):
        S.spread_bp("PEPEUSDT")
    assert S.spread_bp("PEPEUSDT", override={"PEPEUSDT": 3.0}) == 3.0


# --- tick mode ------------------------------------------------------------------------------

def _latency_books():
    # the ask moves from 100.0 to 100.5 during the 0.38 s latency window
    return F.BookTape([book(0.0, 99.9, 100.0), book(0.2, 100.4, 100.5), book(5.0, 100.4, 100.5)])


def test_1_taker_buy_fills_at_the_ask_at_arrival_not_at_decision():
    cfg = F.FillConfig(latency_order_s=0.38)
    f = F.taker_fill(_latency_books(), "BTCUSDT", "buy", 1.0, at(0.0), cfg)
    assert f.price == 100.5 and f.status == "filled"
    assert f.t_arrival_ns == at(0.38)
    f0 = F.taker_fill(_latency_books(), "BTCUSDT", "buy", 1.0, at(0.0), F.FillConfig(latency_order_s=0.0))
    assert f0.price == 100.0


def test_1_taker_sell_fills_at_the_bid_at_arrival():
    f = F.taker_fill(_latency_books(), "BTCUSDT", "sell", 1.0, at(0.0), F.FillConfig())
    assert f.price == 100.4


def test_2_fee_is_fee_taker_times_notional_configurable_and_printed():
    f = F.taker_fill(_latency_books(), "BTCUSDT", "buy", 2.0, at(0.0), F.FillConfig())
    assert f.fee == pytest.approx(0.001 * 2.0 * 100.5)
    g = F.taker_fill(_latency_books(), "BTCUSDT", "buy", 2.0, at(0.0), F.FillConfig(fee_taker=0.00075))
    assert g.fee == pytest.approx(0.00075 * 2.0 * 100.5)
    h = F.FillConfig().header()
    assert h["fee_taker"] == 0.001 and h["fee_maker"] == 0.001
    assert "UNKNOWN" in h["fee_status"]


def test_3_order_larger_than_l1_is_partial_with_the_rest_cancelled_and_flagged():
    tape = F.BookTape([book(0.0, 99.9, 100.0, ask_qty=0.4)])
    f = F.taker_fill(tape, "BTCUSDT", "buy", 1.0, at(0.0), F.FillConfig(latency_order_s=0))
    assert f.qty == 0.4 and f.qty_req == 1.0 and f.price == 100.0
    assert f.status == "partial" and "exceeds_top_level" in f.flags
    assert f.fee == pytest.approx(0.001 * 0.4 * 100.0)


def test_3_penalty_scenario_prices_the_remainder_at_the_p99_spread_and_labels_it():
    tape = F.BookTape([book(0.0, 99.9, 100.0, ask_qty=0.4, sym="ATOMUSDT")])
    cfg = F.FillConfig(latency_order_s=0, beyond_l1="penalty")
    f = F.taker_fill(tape, "ATOMUSDT", "buy", 1.0, at(0.0), cfg)
    worse = 100.0 * (1 + 11.3379 / 1e4)
    assert f.qty == 1.0
    assert f.price == pytest.approx((0.4 * 100.0 + 0.6 * worse) / 1.0)
    assert "exceeds_top_level" in f.flags and "penalty_p99_scenario" in f.flags
    assert any("SCENARIO" in x for x in cfg.labels())


def test_4_maker_buy_does_not_fill_on_a_trade_at_the_limit_but_does_through_it():
    books = F.BookTape([book(0.0, 99.0, 100.0)])
    at_l = F.TradeTape([trade(1.0, 99.5), trade(2.0, 99.5)])
    f = F.maker_order(books, at_l, "BTCUSDT", "buy", 99.5, 1.0, at(0.0), F.FillConfig(), timeout_s=10)
    assert f.status == "cancelled_timeout" and f.qty == 0
    through = F.TradeTape([trade(1.0, 99.5), trade(2.0, 99.49)])
    g = F.maker_order(books, through, "BTCUSDT", "buy", 99.5, 1.0, at(0.0), F.FillConfig(), timeout_s=10)
    assert g.status == "filled" and g.price == 99.5 and g.t_fill_ns == at(2.0)
    assert g.fee == pytest.approx(0.001 * 99.5)


def test_4_maker_sell_mirror():
    books = F.BookTape([book(0.0, 100.0, 101.0)])
    at_l = F.TradeTape([trade(1.0, 100.5)])
    assert F.maker_order(books, at_l, "BTCUSDT", "sell", 100.5, 1.0, at(0), F.FillConfig(),
                         timeout_s=10).status == "cancelled_timeout"
    through = F.TradeTape([trade(1.0, 100.51)])
    g = F.maker_order(books, through, "BTCUSDT", "sell", 100.5, 1.0, at(0), F.FillConfig(), timeout_s=10)
    assert g.status == "filled" and g.price == 100.5


def test_4_touch_is_an_optimistic_labelled_sensitivity_only():
    books = F.BookTape([book(0.0, 99.0, 100.0)])
    at_l = F.TradeTape([trade(1.0, 99.5)])
    cfg = F.FillConfig(maker_rule="touch")
    f = F.maker_order(books, at_l, "BTCUSDT", "buy", 99.5, 1.0, at(0), cfg, timeout_s=10)
    assert f.status == "filled"
    assert any("OPTIMISTIC" in x for x in cfg.labels())
    assert "OPTIMISTIC" in json.dumps(f.to_record())


def test_4_a_trade_before_the_order_arrives_does_not_fill_it():
    books = F.BookTape([book(0.0, 99.0, 100.0)])
    early = F.TradeTape([trade(0.1, 99.0)])       # inside the 0.38 s latency window
    f = F.maker_order(books, early, "BTCUSDT", "buy", 99.5, 1.0, at(0), F.FillConfig(), timeout_s=10)
    assert f.status == "cancelled_timeout"


def test_5_a_maker_order_marketable_on_arrival_is_a_taker_fill():
    books = F.BookTape([book(0.0, 99.0, 100.0), book(0.2, 99.5, 99.6)])
    f = F.maker_order(books, F.TradeTape([]), "BTCUSDT", "buy", 99.8, 1.0, at(0), F.FillConfig(), timeout_s=10)
    assert f.kind == "taker" and f.price == 99.6 and "marketable_on_arrival" in f.flags
    assert f.fee == pytest.approx(0.001 * 99.6)


def test_6_non_fills_are_recorded_and_counted_in_the_fill_rate():
    books = F.BookTape([book(0.0, 99.0, 100.0)])
    trades = F.TradeTape([trade(1.0, 99.4), trade(30.0, 99.0)])
    cfg = F.FillConfig()
    filled = F.maker_order(books, trades, "BTCUSDT", "buy", 99.5, 1.0, at(0), cfg, timeout_s=10)
    timeout = F.maker_order(books, trades, "BTCUSDT", "buy", 99.0, 1.0, at(0), cfg, timeout_s=10)
    cancelled = F.maker_order(books, trades, "BTCUSDT", "buy", 98.9, 1.0, at(0), cfg, timeout_s=60,
                              cancel_ns=at(5.0))
    assert filled.status == "filled"
    assert timeout.status == "cancelled_timeout" and timeout.t_end_ns == at(0.38) + 10 * SEC
    assert cancelled.status == "cancelled_by_strategy" and cancelled.t_end_ns == at(5.0)
    rep = F.fill_report([filled, timeout, cancelled])
    assert rep["maker_orders"] == 3 and rep["maker_filled"] == 1
    assert rep["fill_rate"] == pytest.approx(1 / 3)
    assert rep["status_counts"]["cancelled_timeout"] == 1
    assert timeout.missed is not None and "mid_at_end" in timeout.missed


def test_6_maker_timeout_is_required():
    with pytest.raises(ValueError):
        F.maker_order(F.BookTape([book(0, 99, 100)]), F.TradeTape([]), "BTCUSDT", "buy", 99.5, 1.0,
                      at(0), F.FillConfig())


def test_7_markouts_after_a_maker_fill_equal_hand_computed_values():
    books = F.BookTape([book(0.0, 99.0, 100.0), book(2.5, 98.0, 98.2), book(11.0, 101.0, 101.2),
                        book(70.0, 99.4, 99.6)])
    trades = F.TradeTape([trade(1.5, 99.4)])
    f = F.maker_order(books, trades, "BTCUSDT", "buy", 99.5, 1.0, at(0), F.FillConfig(), timeout_s=10)
    assert f.t_fill_ns == at(1.5)
    m = F.markouts(f, books)
    # +1 s -> t=2.5: mid 98.1; +10 s -> t=11.5: mid 101.1; +60 s -> t=61.5: mid 101.1 (last book <= 61.5)
    assert m["1s"] == pytest.approx((98.1 - 99.5) / 99.5)
    assert m["10s"] == pytest.approx((101.1 - 99.5) / 99.5)
    assert m["60s"] == pytest.approx((101.1 - 99.5) / 99.5)
    sell = F.maker_order(books, F.TradeTape([trade(1.5, 99.6)]), "BTCUSDT", "sell", 99.55, 1.0, at(0),
                         F.FillConfig(), timeout_s=10)
    assert F.markouts(sell, books)["1s"] == pytest.approx(-(98.1 - 99.55) / 99.55)


def test_7_markout_past_the_end_of_data_is_unknown_not_guessed():
    books = F.BookTape([book(0.0, 99.0, 100.0), book(3.0, 99.0, 100.0)])
    f = F.maker_order(books, F.TradeTape([trade(1.5, 99.4)]), "BTCUSDT", "buy", 99.5, 1.0, at(0),
                      F.FillConfig(), timeout_s=10)
    m = F.markouts(f, books)
    assert m["1s"] is not None and m["10s"] is None and m["60s"] is None
    rep = F.fill_report([f])
    assert rep["markout_mean"]["10s"] is None


def test_latency_sweep_runs_the_design_latencies_and_charges_moves_during_latency():
    assert F.LATENCY_SWEEP_S == (0.0, 0.38, 0.75, 1.5)
    tape = F.BookTape([book(0.0, 99.9, 100.0), book(0.5, 100.1, 100.2), book(1.0, 100.3, 100.4)])
    out = F.latency_sweep(lambda cfg: F.taker_fill(tape, "BTCUSDT", "buy", 1.0, at(0), cfg).price,
                          F.FillConfig())
    assert out == {0.0: 100.0, 0.38: 100.0, 0.75: 100.2, 1.5: 100.4}


def test_no_book_at_arrival_is_a_recorded_non_fill():
    tape = F.BookTape([book(5.0, 99.9, 100.0)])
    f = F.taker_fill(tape, "BTCUSDT", "buy", 1.0, at(0.0), F.FillConfig())
    assert f.status == "no_book" and f.qty == 0 and f.fee == 0


# --- 9: clock discipline over real recorded messages -----------------------------------------

def _fixture_ticks():
    """Real bookTicker/aggTrade messages from the stream fixture. The fixture has no receive stamps, so
    receive times are assigned in file order (50 ms apart); the prices and quantities are the real ones."""
    out = []
    for i, line in enumerate(FIXTURE.read_text(encoding="utf-8").splitlines()):
        t = parse(line, T0 + i * 50_000_000)
        if t is not None and t.kind in ("book", "trade"):
            out.append(t)
    return out


def test_9_no_fill_uses_a_book_record_received_after_the_order_arrived():
    ticks = _fixture_ticks()
    books, trades = F.BookTape(ticks), F.TradeTape(ticks)
    syms = sorted({t.symbol for t in ticks if t.kind == "book"})
    assert syms
    rng = random.Random(7)
    span = ticks[-1].ts_recv_ns - T0
    checked = 0
    for _ in range(400):
        sym = rng.choice(syms)
        t = T0 + rng.randrange(-SEC, span + SEC)
        lat = rng.choice(F.LATENCY_SWEEP_S)
        cfg = F.FillConfig(latency_order_s=lat)
        side = rng.choice(("buy", "sell"))
        f = F.taker_fill(books, sym, side, rng.choice((1e-4, 1.0, 1e6)), t, cfg)
        if f.book_ts_ns is not None:
            assert f.book_ts_ns <= f.t_arrival_ns
            checked += 1
        b = books.at(sym, t + int(lat * SEC))
        if b is not None:
            lim = b.data["bid"] if side == "buy" else b.data["ask"]
            m = F.maker_order(books, trades, sym, side, lim, 1.0, t, cfg, timeout_s=0.5)
            assert m.book_ts_ns is None or m.book_ts_ns <= m.t_arrival_ns
            if m.t_fill_ns is not None:
                assert m.t_fill_ns > m.t_arrival_ns
    assert checked > 100


# --- 8: kline (replay) mode --------------------------------------------------------------------

def bar1m(sym: str, open_ms: int, o, h=None, l=None, c=None) -> B.Bar:
    c = o if c is None else c
    return B.Bar(sym, "1m", open_ms, open_ms + MIN, o, h if h is not None else max(o, c),
                 l if l is not None else min(o, c), c, 1.0, c, 1, 0.5)


def test_8_bar_taker_price_is_open_plus_or_minus_half_the_measured_median_spread():
    cfg = F.FillConfig()
    assert F.bar_taker_price("DOTUSDT", "buy", 10.0, cfg) == pytest.approx(10.0 * (1 + 8.3717 / 2 / 1e4))
    assert F.bar_taker_price("DOTUSDT", "sell", 10.0, cfg) == pytest.approx(10.0 * (1 - 8.3717 / 2 / 1e4))
    p90 = F.FillConfig(spread_stat="p90")
    assert F.bar_taker_price("DOTUSDT", "buy", 10.0, p90) == pytest.approx(10.0 * (1 + 8.4782 / 2 / 1e4))
    with pytest.raises(S.UnknownSpreadError):
        F.bar_taker_price("PEPEUSDT", "buy", 1.0, cfg)


def test_8_bar_limit_fills_only_when_the_bar_trades_strictly_through():
    cfg = F.FillConfig()
    assert F.bar_limit_fill(bar1m("X", 0, 10, h=11, l=9.5, c=10), "buy", 9.5, cfg) is None
    assert F.bar_limit_fill(bar1m("X", 0, 10, h=11, l=9.49, c=10), "buy", 9.5, cfg) == 9.5
    assert F.bar_limit_fill(bar1m("X", 0, 10, h=11, l=9, c=10), "sell", 11, cfg) is None
    assert F.bar_limit_fill(bar1m("X", 0, 10, h=11.01, l=9, c=10), "sell", 11, cfg) == 11
    assert F.bar_limit_fill(bar1m("X", 0, 10, h=11, l=9.5, c=10), "buy", 9.5, F.FillConfig(maker_rule="touch")) == 9.5


def test_8_a_bar_hitting_both_stop_and_take_profit_books_the_stop():
    cfg = F.FillConfig()
    b = bar1m("BTCUSDT", 0, 100, h=103, l=97, c=100)
    reason, price = F.bar_exit(b, "long", stop=98, take_profit=102, cfg=cfg)
    assert reason == "stop" and price == pytest.approx(98 * (1 - 0.0012 / 2 / 1e4))
    s_reason, _ = F.bar_exit(b, "short", stop=102, take_profit=98, cfg=cfg)
    assert s_reason == "stop"


def test_8_a_gap_through_the_stop_fills_at_the_open_and_tp_needs_trade_through():
    cfg = F.FillConfig()
    gap = bar1m("BTCUSDT", 0, 95, h=96, l=94, c=95)
    reason, price = F.bar_exit(gap, "long", stop=98, take_profit=110, cfg=cfg)
    assert reason == "stop" and price == pytest.approx(95 * (1 - 0.0012 / 2 / 1e4))
    assert F.bar_exit(bar1m("BTCUSDT", 0, 100, h=102, l=99, c=101), "long", 98, 102, cfg) is None
    assert F.bar_exit(bar1m("BTCUSDT", 0, 100, h=102.5, l=99, c=101), "long", 98, 102, cfg) == ("take_profit", 102)


def _two_day_stream(sym="BTCUSDT"):
    d0 = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    return {sym: [bar1m(sym, d0 + DAY - MIN, 100), bar1m(sym, d0 + DAY, 101, c=105),
                  bar1m(sym, d0 + 2 * DAY - MIN, 110), bar1m(sym, d0 + 2 * DAY, 111),
                  bar1m(sym, d0 + 3 * DAY - MIN, 111)]}


class OpenThenClose:
    name, params, intervals = "otc", {}, ("1d",)

    def __init__(self):
        self.n = 0

    def on_bar(self, bar, ctx):
        self.n += 1
        act = "open" if self.n == 1 else "close" if self.n == 2 else None
        return [Signal("otc", bar.symbol, act, ctx.now_ms, "t", bar.close, "1d")] if act else []


def test_8_replay_engine_uses_the_kline_broker_at_next_open_with_measured_spread_and_fee():
    cfg = F.FillConfig(fill_cap_usd=None)
    acc = KlineBroker(1000, 1, cfg, name="net")
    replay(_two_day_stream("DOTUSDT"), [OpenThenClose()], {"net": acc})
    (t,) = acc.trades
    half = 8.3717 / 2 / 1e4
    buy, sell = 101 * (1 + half), 111 * (1 - half)
    assert t["entry"] == pytest.approx(buy) and t["exit"] == pytest.approx(sell)
    qty = 1000 / (buy * 1.001)
    assert acc.cash == pytest.approx(qty * sell * 0.999)
    assert "depth_unmodelled" in acc.labels()


def test_8_kline_broker_refuses_an_unmeasured_symbol_unless_overridden():
    with pytest.raises(S.UnknownSpreadError):
        KlineBroker(1000, 1, F.FillConfig(), symbols=["PEPEUSDT"])
    acc = KlineBroker(1000, 1, F.FillConfig(fill_cap_usd=None, spread_override={"PEPEUSDT": 2.0}),
                      symbols=["PEPEUSDT"])
    replay(_two_day_stream("PEPEUSDT"), [OpenThenClose()], {"x": acc})
    assert acc.trades[0]["entry"] == pytest.approx(101 * (1 + 1e-4))
    assert any("override" in x for x in acc.labels())


def test_8_per_fill_cap_clips_by_default_and_labels_when_disabled():
    acc = KlineBroker(1000, 1, F.FillConfig(), name="capped")          # default cap: $5 per fill
    replay(_two_day_stream(), [OpenThenClose()], {"c": acc})
    ev = [e for e in acc.events if e["type"] == "signal"][0]
    assert ev["payload"]["qty"] * ev["payload"]["entry"] * 1.001 == pytest.approx(5.0)
    assert "capped_at_fill_cap" in ev["payload"]["flags"]
    lab = KlineBroker(1000, 1, F.FillConfig(over_cap="label"))
    replay(_two_day_stream(), [OpenThenClose()], {"l": lab})
    ev = [e for e in lab.events if e["type"] == "signal"][0]
    assert "depth_unmodelled" in ev["payload"]["flags"] and "depth_unmodelled" in lab.labels()


def test_gross_scenario_has_no_fee_and_no_spread():
    acc = KlineBroker(1000, 1, F.FillConfig(fee_taker=0, fee_maker=0, spread_stat="none", fill_cap_usd=None))
    replay(_two_day_stream(), [OpenThenClose()], {"g": acc})
    assert acc.cash == pytest.approx(1000 * 111 / 101)


def test_legacy_cost_model_account_is_unchanged():
    acc = PaperAccount(1000, 1, CostModel(fee_rate=0.001, spread_bps=2, slippage_bps=2))
    replay(_two_day_stream(), [OpenThenClose()], {"net": acc})
    (t,) = acc.trades
    assert t["entry"] == pytest.approx(101 * (1 + 3e-4))


# --- 11: labels -------------------------------------------------------------------------------

def test_11_every_fill_record_is_paper_and_carries_its_fill_model_parameters():
    f = F.taker_fill(_latency_books(), "BTCUSDT", "buy", 1.0, at(0.0), F.FillConfig())
    r = f.to_record()
    assert r["paper"] is True and r["mode"] == "PAPER"
    assert r["fill_model"]["latency_order_s"] == 0.38 and r["fill_model"]["maker_rule"] == "trade_through"
    acc = KlineBroker(1000, 1, F.FillConfig())
    replay(_two_day_stream(), [OpenThenClose()], {"n": acc})
    fills = [e for e in acc.events if e["type"] in ("signal", "close")]
    assert fills and all(e["payload"]["paper"] is True and "fill_model" in e["payload"] for e in fills)


# --- 10: import guard ----------------------------------------------------------------------------

def test_10_paper_package_never_imports_an_exchange_or_live_client():
    from conftest import BLOCKED_MODULES
    code = (
        "import sys, trading.paper, trading.paper.fills, trading.paper.spreads, trading.paper.broker;"
        "import json; print(json.dumps(sorted(sys.modules)))"
    )
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
    mods = json.loads(out.stdout)
    bad = [m for m in mods if m == "ccxt" or m.startswith("ccxt.") or m.startswith("trading.exchange")
           or m in BLOCKED_MODULES or m.startswith("websockets") or m.startswith("aiohttp")]
    assert bad == []


# --- the replay CLI with the paper broker ---------------------------------------------------------

def _history(root: Path, sym: str, months: list[str]) -> None:
    import zipfile
    for i, ym in enumerate(months):
        y, m = map(int, ym.split("-"))
        d0 = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)
        rows = []
        for d in range(28):
            px = 100 + i * 10 + d                      # a steady uptrend: the SMA filter turns on
            for k in (0, 1439):
                o = d0 + d * DAY + k * MIN
                rows.append(f"{o},{px},{px},{px},{px},1,{o + MIN - 1},{px},3,0.5,{px / 2},0")
        p = root / "klines_1m" / sym / f"{sym}-1m-{ym}.zip"
        p.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(p.name.replace(".zip", ".csv"), "\n".join(rows) + "\n")


def _cli_args(root: Path, out: Path, syms: str) -> list[str]:
    return ["--history-root", str(root), "--symbols", syms, "--warmup-start", "2021-01", "--trade-start",
            "2021-02", "--is-oos-split", "2021-03", "--wf-fit", "1", "--wf-trade", "1", "--sma", "5",
            "--btc-sma", "5", "--out", str(out)]


def test_replay_cli_runs_the_baseline_through_the_paper_broker(tmp_path, capsys):
    from trading.replay import run
    months = [f"2021-{m:02d}" for m in range(1, 10)]
    for s in ("BTCUSDT", "ETHUSDT"):
        _history(tmp_path / "h", s, months)
    summ = run.main(_cli_args(tmp_path / "h", tmp_path / "o", "BTCUSDT,ETHUSDT")
                    + ["--broker", "paper", "--fill-cap-usd", "none"])
    assert summ["broker"] == "paper" and summ["run_id"].endswith("-paper")
    assert summ["costs"]["net"]["spread_stat"] == "median" and summ["costs"]["stress"]["spread_stat"] == "p90"
    assert summ["costs"]["gross"]["fee_taker"] == 0.0 and summ["costs"]["net"]["fee_taker"] == 0.001
    assert "depth_unmodelled" in summ["fill_model_labels"]["net"]
    assert summ["scenarios_consistent"] and summ["fills"]["net"] > 0
    ev = [json.loads(x) for x in (tmp_path / "o" / summ["run_id"] / "events.jsonl").read_text().splitlines()]
    fills = [e for e in ev if e["type"] in ("signal", "close")]
    assert fills and all(e["payload"]["paper"] is True for e in fills)
    assert "[fill model]" in capsys.readouterr().out


def test_replay_cli_refuses_an_unmeasured_symbol_with_the_paper_broker(tmp_path):
    from trading.replay import run
    months = [f"2021-{m:02d}" for m in range(1, 10)]
    for s in ("BTCUSDT", "FOOUSDT"):
        _history(tmp_path / "h", s, months)
    with pytest.raises(S.UnknownSpreadError):
        run.main(_cli_args(tmp_path / "h", tmp_path / "o", "BTCUSDT,FOOUSDT") + ["--broker", "paper"])
