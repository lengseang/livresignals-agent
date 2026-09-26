"""T3.4: stop/take-profit/time-stop exits, paper-only shorts, and the four audit TEST plugins.

Design and pre-registration: docs/loop/designs/T3.4.md. REPLAY/PAPER only; nothing here is evidence of edge.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading.paper import fills as F
from trading.paper.broker import KlineBroker
from trading.replay import bars as B
from trading.replay import gate
from trading.replay.broker import CostModel, PaperAccount
from trading.replay.context import BarStore, Context, ReplayClock
from trading.replay.engine import replay
from trading.replay.strategy import Signal

MIN = 60_000
HOUR = 3_600_000
DAY = 86_400_000
REPO = Path(__file__).resolve().parents[2]


def ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


T0 = ms("2021-01-01T00:00:00")


def bar1m(sym, open_ms, o, h=None, l=None, c=None, v=1.0) -> B.Bar:
    c = o if c is None else c
    return B.Bar(sym, "1m", open_ms, open_ms + MIN, o, h if h is not None else max(o, c),
                 l if l is not None else min(o, c), c, v, v * c, 1, v / 2)


def hourly(sym, closes, vols=None, highs=None, skip=(), t0=T0):
    """One 1m bar per hour, the hour's last minute, so each 1h bar equals that minute's bar."""
    out = []
    for i, c in enumerate(closes):
        if i in skip:
            continue
        v = 1.0 if vols is None else vols[i]
        h = c if highs is None else highs[i]
        out.append(bar1m(sym, t0 + i * HOUR + 59 * MIN, c, h=h, l=c, c=c, v=v))
    return out


def daily(sym, closes, t0=T0):
    return [bar1m(sym, t0 + d * DAY + DAY - MIN, c) for d, c in enumerate(closes)]


def run_signals(streams, strat, slots=100):
    acc = PaperAccount(1000, slots, CostModel(0, 0, 0), name="net")
    res = replay(streams, [strat], {"net": acc})
    return [(e["ts_ms"], e["payload"]["symbol"], e["payload"]["action"], e["payload"]["side"])
            for e in res.signal_events], acc, res


def noise(n, base=100.0):
    return [base * (1.001 if i % 2 else 1.0) for i in range(n)]


# ================================================================ Signal: backwards compatible

def test_signal_without_exit_fields_still_builds_and_defaults_to_none():
    s = Signal("x", "BTCUSDT", "open", 0, "t", 1.0, "1d")
    assert s.side == "L" and s.sl is None and s.tp is None and s.max_hold_ms is None
    s2 = Signal("x", "BTCUSDT", "open", 0, "t", 1.0, "1h", sl=0.01, tp=0.02, max_hold_ms=HOUR)
    assert (s2.sl, s2.tp, s2.max_hold_ms) == (0.01, 0.02, HOUR)
    assert s2.record("REPLAY")["payload"]["sl"] == 0.01


# ================================================================ broker: SL / TP / time stop

def _long(acc, sl=0.01, tp=0.02, hold=None, sym="BTCUSDT", t=0, o=100.0):
    acc.submit(Signal("x", sym, "open", t, "t", o, "1h", sl=sl, tp=tp, max_hold_ms=hold))
    acc.on_open(bar1m(sym, t, o))
    return acc.positions[sym]


def test_long_stop_loss_fills_at_the_stop_as_a_taker():
    acc = PaperAccount(1000, 1, CostModel(0, 0, 0))
    p = _long(acc)
    assert p["stop"] == pytest.approx(99) and p["take_profit"] == pytest.approx(102)
    acc.mark(bar1m("BTCUSDT", MIN, 100, h=100.5, l=98.5, c=99.5))
    (t,) = acc.trades
    assert t["exit"] == pytest.approx(99) and t["why"] == "stop_loss" and "BTCUSDT" not in acc.positions
    assert t["closed_ms"] == 2 * MIN                              # known at that bar's close


def test_take_profit_needs_a_strict_trade_through_and_fills_at_the_limit():
    acc = PaperAccount(1000, 1, CostModel(0, 0, 0))
    _long(acc)
    acc.mark(bar1m("BTCUSDT", MIN, 100, h=102, l=100, c=101))    # touch only: no fill
    assert acc.trades == []
    acc.mark(bar1m("BTCUSDT", 2 * MIN, 101, h=102.01, l=101, c=102))
    (t,) = acc.trades
    assert t["exit"] == pytest.approx(102) and t["why"] == "take_profit"


def test_a_bar_touching_both_books_the_stop():
    acc = PaperAccount(1000, 1, CostModel(0, 0, 0))
    _long(acc)
    acc.mark(bar1m("BTCUSDT", MIN, 100, h=103, l=98, c=100))
    assert acc.trades[0]["why"] == "stop_loss"


def test_a_gap_through_the_stop_fills_at_the_open():
    acc = PaperAccount(1000, 1, CostModel(0, 0, 0))
    _long(acc)
    acc.mark(bar1m("BTCUSDT", MIN, 97, h=97.5, l=96, c=97))
    assert acc.trades[0]["exit"] == pytest.approx(97)


def test_exit_checks_never_use_a_bar_that_opened_before_the_fill():
    acc = PaperAccount(1000, 1, CostModel(0, 0, 0))
    _long(acc, t=10 * MIN)
    acc.mark(bar1m("BTCUSDT", 9 * MIN, 100, h=100, l=50, c=100))  # opened before the fill
    assert acc.trades == [] and "BTCUSDT" in acc.positions


def test_time_stop_closes_at_the_next_1m_open():
    acc = PaperAccount(1000, 1, CostModel(0, 0, 0))
    _long(acc, sl=None, tp=None, hold=2 * MIN)
    acc.mark(bar1m("BTCUSDT", 0, 100, c=101))                     # ends at 1m: before the deadline
    assert not acc.pending
    acc.mark(bar1m("BTCUSDT", MIN, 101, c=104))                   # ends at 2m: deadline reached
    assert acc.trades == [] and acc.pending
    acc.on_open(bar1m("BTCUSDT", 2 * MIN, 105))
    (t,) = acc.trades
    assert t["exit"] == pytest.approx(105) and t["why"] == "time_stop"


def test_kline_broker_exits_are_priced_by_the_fill_model_and_tp_pays_the_maker_fee():
    cfg = F.FillConfig(fee_taker=0.001, fee_maker=0.0005, fill_cap_usd=None)
    acc = KlineBroker(1000, 1, cfg)
    _long(acc, sym="DOTUSDT")
    b = bar1m("DOTUSDT", MIN, 100, h=100.5, l=98.5, c=99.5)
    acc.mark(b)
    (t,) = acc.trades
    assert t["exit"] == pytest.approx(F.bar_exit(b, "long", 99.0, 102.0, cfg)[1])

    acc2 = KlineBroker(1000, 1, cfg)
    p = _long(acc2, sym="DOTUSDT")
    qty, entry_fee = p["qty"], p["fee"]
    acc2.mark(bar1m("DOTUSDT", MIN, 100, h=102.5, l=100, c=102))
    (t2,) = acc2.trades
    assert t2["exit"] == pytest.approx(102) and t2["fee"] == pytest.approx(entry_fee + qty * 102 * 0.0005)


def test_engine_checks_the_entry_bar_itself_after_the_fill():
    class OpenOnce:
        name, params, intervals = "o", {}, ("1d",)

        def on_bar(self, bar, ctx):
            if ctx.position(bar.symbol) is None:
                return [Signal("o", bar.symbol, "open", ctx.now_ms, "t", bar.close, "1d", sl=0.01, tp=0.02)]
            return []

    bars = [bar1m("BTCUSDT", T0 + DAY - MIN, 100), bar1m("BTCUSDT", T0 + DAY, 100, h=100.2, l=98, c=99)]
    acc = PaperAccount(1000, 1, CostModel(0, 0, 0))
    replay({"BTCUSDT": bars}, [OpenOnce()], {"net": acc})
    (t,) = acc.trades
    assert t["exit"] == pytest.approx(99) and t["opened_ms"] == T0 + DAY


# ================================================================ broker: paper-only shorts

def _short(acc, sl=None, tp=None, o=100.0):
    acc.submit(Signal("x", "BTCUSDT", "open", 0, "t", o, "1d", side="S", sl=sl, tp=tp))
    acc.on_open(bar1m("BTCUSDT", 0, o))
    return acc.positions["BTCUSDT"]


def test_short_arithmetic_marking_and_negative_qty_in_ctx():
    acc = PaperAccount(1000, 1, CostModel(0.001, 0, 0))
    p = _short(acc)
    qty = 1000 / (100 * 1.001)
    assert p["qty"] == pytest.approx(qty) and p["side"] == "S"
    ctx = Context(BarStore(), ReplayClock(), acc)
    assert ctx.position("BTCUSDT") == (pytest.approx(-qty), 100)
    acc.mark(bar1m("BTCUSDT", MIN, 90))
    assert acc.equity() == pytest.approx(qty * (200 - 90))
    acc.submit(Signal("x", "BTCUSDT", "close", 2 * MIN, "t", 90, "1d", side="S"))
    acc.on_open(bar1m("BTCUSDT", 2 * MIN, 90))
    (t,) = acc.trades
    assert acc.cash == pytest.approx(qty * (200 - 90) - qty * 90 * 0.001)
    assert t["gross_ret"] == pytest.approx(0.10) and t["net_ret"] == pytest.approx(acc.cash / 1000 - 1)
    assert t["side"] == "S"


def test_short_stop_and_take_profit_mirror_the_long_rules():
    acc = PaperAccount(1000, 1, CostModel(0, 0, 0))
    p = _short(acc, sl=0.05, tp=0.10)
    assert p["stop"] == pytest.approx(105) and p["take_profit"] == pytest.approx(90)
    acc.mark(bar1m("BTCUSDT", MIN, 100, h=106, l=99, c=104))
    assert acc.trades[0]["exit"] == pytest.approx(105) and acc.trades[0]["why"] == "stop_loss"
    acc2 = PaperAccount(1000, 1, CostModel(0, 0, 0))
    _short(acc2, sl=0.05, tp=0.10)
    acc2.mark(bar1m("BTCUSDT", MIN, 95, h=95, l=90, c=91))        # touch: no fill
    assert acc2.trades == []
    acc2.mark(bar1m("BTCUSDT", 2 * MIN, 91, h=91, l=89.9, c=90))
    assert acc2.trades[0]["exit"] == pytest.approx(90) and acc2.trades[0]["why"] == "take_profit"


# ================================================================ no-look-ahead helper

def assert_no_lookahead(make_strat, streams, cut_ms, seed=7):
    """Signals at or before cut_ms must not change when every bar opening at/after cut_ms is perturbed."""
    base, _, _ = run_signals(streams, make_strat())
    rng = random.Random(seed)
    pert = {}
    for s, bars in streams.items():
        out = []
        for b in bars:
            if b.open_ms >= cut_ms:
                k = rng.uniform(0.5, 1.5)
                out.append(B.Bar(b.symbol, b.interval, b.open_ms, b.end_ms, b.open * k, b.high * k * 1.2,
                                 b.low * k * 0.8, b.close * k, b.volume * rng.uniform(0.1, 10), b.quote_volume,
                                 b.trades, b.taker_buy_base))
            else:
                out.append(b)
        pert[s] = out
    other, _, _ = run_signals(pert, make_strat())
    before = [x for x in base if x[0] <= cut_ms]
    assert before, "fixture must produce signals before the cut"
    assert before == [x for x in other if x[0] <= cut_ms]


def random_walk(n, seed, base=100.0, vol=0.02):
    rng = random.Random(seed)
    out, p = [], base
    for _ in range(n):
        p *= 1 + rng.gauss(0, vol)
        out.append(p)
    return out


# ================================================================ intraday momentum / reversal

def test_intraday_momentum_fires_on_a_z_move_up_and_only_there():
    from trading.replay.plugins.intraday import IntradayMomentum
    closes = noise(200) + [103.0] * 3
    sig, _, _ = run_signals({"BTCUSDT": hourly("BTCUSDT", closes)},
                            IntradayMomentum(lookback_h=4, z=2.0, vol_h=168, sl=0.01, tp=0.02, max_hold_h=24))
    assert [s[0] for s in sig] == [T0 + 201 * HOUR]            # the 1h bar with index 200 ends at 201h
    assert sig[0][2:] == ("open", "L")


def test_intraday_momentum_signal_carries_its_exits():
    from trading.replay.plugins.intraday import IntradayMomentum
    closes = noise(200) + [103.0]
    _, _, res = run_signals({"BTCUSDT": hourly("BTCUSDT", closes)},
                            IntradayMomentum(lookback_h=4, sl=0.02, tp=0.04, max_hold_h=72))
    p = res.signal_events[0]["payload"]
    assert (p["sl"], p["tp"], p["max_hold_ms"]) == (0.02, 0.04, 72 * HOUR)


def test_intraday_reversal_fires_on_a_z_move_down_not_up():
    from trading.replay.plugins.intraday import IntradayReversal
    up = noise(200) + [103.0]
    down = noise(200) + [97.0]
    mk = lambda: IntradayReversal(lookback_h=4, z=2.0, vol_h=168, sl=0.01, tp=0.02, max_hold_h=24)
    assert run_signals({"BTCUSDT": hourly("BTCUSDT", up)}, mk())[0] == []
    sig, _, _ = run_signals({"BTCUSDT": hourly("BTCUSDT", down)}, mk())
    assert [s[0] for s in sig] == [T0 + 201 * HOUR]


def test_intraday_warmup_and_a_missing_hour_emit_nothing():
    from trading.replay.plugins.intraday import IntradayMomentum
    mk = lambda: IntradayMomentum(lookback_h=4, z=2.0, vol_h=168)
    short = noise(100) + [103.0]                                 # fewer than 169 hourly bars
    assert run_signals({"BTCUSDT": hourly("BTCUSDT", short)}, mk())[0] == []
    gap = noise(200) + [103.0]
    assert run_signals({"BTCUSDT": hourly("BTCUSDT", gap, skip={198})}, mk())[0] == []


def test_intraday_plugins_never_look_ahead():
    from trading.replay.plugins.intraday import IntradayMomentum, IntradayReversal
    streams = {s: hourly(s, random_walk(600, seed)) for s, seed in (("BTCUSDT", 1), ("ETHUSDT", 2))}
    cut = T0 + 400 * HOUR
    assert_no_lookahead(lambda: IntradayMomentum(lookback_h=4, z=1.0, vol_h=168), streams, cut)
    assert_no_lookahead(lambda: IntradayReversal(lookback_h=24, z=1.0, vol_h=168), streams, cut)


# ================================================================ range breakout

def _flat(n, c=100.0, h=100.5):
    return [c] * n, [1.0] * n, [h] * n


def test_range_breakout_needs_a_close_above_the_prior_range_and_volume():
    from trading.replay.plugins.range_breakout import RangeBreakout
    mk = lambda: RangeBreakout(interval="1h", n=24, vol_mult=2.0, sl=0.01, tp=0.02, max_hold_h=24)
    for close, vol, fires in ((101.0, 3.0, True), (101.0, 1.5, False), (100.4, 3.0, False)):
        c, v, h = _flat(30)
        c, v, h = c + [close], v + [vol], h + [close]
        sig, _, _ = run_signals({"ETHUSDT": hourly("ETHUSDT", c, vols=v, highs=h)}, mk())
        assert ([s[0] for s in sig] == [T0 + 31 * HOUR]) is fires, (close, vol)


def test_range_breakout_interval_is_a_parameter_and_gaps_emit_nothing():
    from trading.replay.plugins.range_breakout import RangeBreakout
    assert RangeBreakout(interval="4h", n=30).intervals == ("4h",)
    c, v, h = _flat(30)
    c, v, h = c + [101.0], v + [3.0], h + [101.0]
    sig, _, _ = run_signals({"ETHUSDT": hourly("ETHUSDT", c, vols=v, highs=h, skip={20})},
                            RangeBreakout(interval="1h", n=24))
    assert sig == []


def test_range_breakout_never_looks_ahead():
    from trading.replay.plugins.range_breakout import RangeBreakout
    rng = random.Random(3)
    closes = random_walk(600, 4, vol=0.01)
    vols = [rng.uniform(0.2, 5) for _ in closes]
    highs = [c * 1.002 for c in closes]
    streams = {"SOLUSDT": hourly("SOLUSDT", closes, vols=vols, highs=highs)}
    assert_no_lookahead(lambda: RangeBreakout(interval="1h", n=12, vol_mult=1.5), streams, T0 + 400 * HOUR)


# ================================================================ regime long/short

def _regime(**kw):
    from trading.replay.plugins.regime_ls import RegimeLS
    return RegimeLS(**{"sma_regime": 5, "sma_trend": 3, "mom_days": 2, "topn": 2, **kw})


def test_regime_bull_goes_long_the_strongest_coins_above_their_sma():
    n = 12
    streams = {"BTCUSDT": daily("BTCUSDT", [100 * 1.03 ** i for i in range(n)]),
               "AUSDT": daily("AUSDT", [100 * 1.05 ** i for i in range(n)]),     # strongest
               "BUSDT": daily("BUSDT", [100 * 1.01 ** i for i in range(n)]),
               "CUSDT": daily("CUSDT", [100 * 0.97 ** i for i in range(n)])}     # below its SMA
    sig, acc, _ = run_signals(streams, _regime(), slots=2)
    opens = [(s[1], s[3]) for s in sig if s[2] == "open"]
    first_day = min(s[0] for s in sig)
    assert sorted(x[1] for x in sig if x[0] == first_day) == ["AUSDT", "BTCUSDT"]
    assert all(side == "L" for _, side in opens) and "CUSDT" not in {s for s, _ in opens}


def test_regime_bear_shorts_the_weakest_coins_paper_only():
    n = 12
    streams = {"BTCUSDT": daily("BTCUSDT", [200 - 3 * i for i in range(n)]),
               "AUSDT": daily("AUSDT", [100 * 1.02 ** i for i in range(n)]),
               "BUSDT": daily("BUSDT", [100 * 0.99 ** i for i in range(n)]),
               "CUSDT": daily("CUSDT", [100 * 0.95 ** i for i in range(n)])}     # weakest
    sig, acc, _ = run_signals(streams, _regime(), slots=2)
    first_day = min(s[0] for s in sig)
    day1 = sorted((s[1], s[2], s[3]) for s in sig if s[0] == first_day)
    assert day1 == [("BTCUSDT", "open", "S"), ("CUSDT", "open", "S")]
    assert any(p["side"] == "S" for p in acc.positions.values())
    assert any("PAPER-ONLY" in c for c in _regime().caveats)


def test_regime_long_exits_when_the_coin_breaks_its_sma():
    closes_a = [100 * 1.05 ** i for i in range(10)] + [80, 80]
    streams = {"BTCUSDT": daily("BTCUSDT", [100 + i for i in range(12)]),
               "AUSDT": daily("AUSDT", closes_a)}
    sig, acc, _ = run_signals(streams, _regime(topn=1), slots=1)
    a = [(s[0], s[2]) for s in sig if s[1] == "AUSDT"]
    assert a[0][1] == "open" and ("close" in [x[1] for x in a])
    close_t = [t for t, act in a if act == "close"][0]
    assert close_t == T0 + 11 * DAY                               # the day A closed at 80 (< SMA3)


def test_regime_ls_never_looks_ahead():
    streams = {s: daily(s, random_walk(150, seed, vol=0.04)) for s, seed in
               (("BTCUSDT", 11), ("ETHUSDT", 12), ("SOLUSDT", 13), ("XRPUSDT", 14))}
    assert_no_lookahead(lambda: _regime(sma_regime=20, sma_trend=10, mom_days=5, topn=2), streams, T0 + 100 * DAY)


# ================================================================ gate, registry, CLI

def test_gate_round_trip_cost_understands_the_paper_broker_header():
    hdr = F.FillConfig().header()
    c = gate.round_trip_cost(hdr, ["BTCUSDT", "DOTUSDT"])
    assert c == pytest.approx(2 * (0.001 + 8.3717 / 2 / 1e4))       # the widest symbol: conservative
    gross = F.FillConfig(fee_taker=0, fee_maker=0, spread_stat="none").header()
    assert gate.round_trip_cost(gross, ["DOTUSDT"]) == 0
    assert gate.round_trip_cost({"fee_rate": 0.001, "spread_bps": 2, "slippage_bps": 2}) == pytest.approx(0.0026)


def test_registry_has_the_four_t34_plugins_pointing_at_the_design():
    from trading.scanner import registry as R
    items = {p.name: p for p in R.DEFAULT.list()}
    for name in ("intraday_momentum", "intraday_reversal", "range_breakout", "regime_ls"):
        assert name in items
        assert (REPO / items[name].evidence.split("#")[0]).exists()
        s = R.DEFAULT.build(name)
        assert s.name == name and s.intervals


def _history(root: Path, syms, months):
    import zipfile
    rng = random.Random(5)
    for sym in syms:
        px = 100.0
        for ym in months:
            y, m = map(int, ym.split("-"))
            d0 = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)
            rows = []
            for d in range(28):
                px *= 1 + rng.gauss(0.002, 0.03)
                for k in (0, 1439):
                    o = d0 + d * DAY + k * MIN
                    rows.append(f"{o},{px},{px * 1.01},{px * 0.99},{px},1,{o + MIN - 1},{px},3,0.5,{px / 2},0")
            p = root / "klines_1m" / sym / f"{sym}-1m-{ym}.zip"
            p.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(p, "w") as z:
                z.writestr(p.name.replace(".zip", ".csv"), "\n".join(rows) + "\n")


def test_run_cli_runs_a_registry_strategy_with_params_and_a_tag(tmp_path):
    from trading.replay import run
    months = [f"2021-{m:02d}" for m in range(1, 10)]
    _history(tmp_path / "h", ["BTCUSDT", "ETHUSDT", "SOLUSDT"], months)
    summ = run.main(["--history-root", str(tmp_path / "h"), "--symbols", "BTCUSDT,ETHUSDT,SOLUSDT",
                     "--warmup-start", "2021-01", "--trade-start", "2021-02", "--is-oos-split", "2021-05",
                     "--wf-fit", "1", "--wf-trade", "1", "--out", str(tmp_path / "o"), "--broker", "paper",
                     "--fill-cap-usd", "none", "--slots", "2", "--strategy", "regime_ls", "--tag", "RLx",
                     "--params", json.dumps({"sma_regime": 10, "sma_trend": 5, "mom_days": 3, "topn": 2,
                                             "sl": 0.1, "tp": 0.2})])
    assert summ["strategy"] == "regime_ls" and summ["run_id"].endswith("-RLx")
    assert summ["params"]["sma_regime"] == 10 and summ["params"]["sl"] == 0.1
    assert any("PAPER-ONLY" in c for c in summ["caveats"])
    assert not any("SMA50 was chosen" in c for c in summ["caveats"])
    trial = json.loads((tmp_path / "o" / "trials.jsonl").read_text().splitlines()[-1])
    assert trial["strategy"] == "regime_ls" and trial["run_id"] == summ["run_id"]
    rep = gate.evaluate(tmp_path / "o" / summ["run_id"])
    assert rep["verdict"] in (gate.PASS, gate.FAIL, gate.INSUFFICIENT)


# ================================================================ loader: late-closing traded bars (found by the T3.4 runs)

def test_loader_drops_and_counts_a_traded_bar_closing_up_to_2s_late_and_still_rejects_worse(tmp_path):
    """Measured: LTCUSDT-1m-2018-02 has open 1518321540000, close_time 1518321600999 (1 s past the bar), 28 trades.
    Keeping its real end would overlap the next bar; clamping it would leak up to 2 s of the future. It is dropped
    (a 1-minute data gap, no look-ahead) and counted."""
    import zipfile

    from trading.replay import loader

    def z(path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as f:
            f.writestr(path.name.replace(".zip", ".csv"), "\n".join(rows) + "\n")
        return path

    t = 1518321540000
    ok = f"{t - MIN},1,1,1,1,1,{t - 1},1,3,0.5,0.5,0"
    late = f"{t},133,134,132,133.4,133.43915,1518321600999,1,28,0.5,0.5,0"
    nxt = f"{t + MIN},2,2,2,2,1,{t + 2 * MIN - 1},1,3,0.5,0.5,0"
    stats = {}
    got = list(loader.iter_klines(z(tmp_path / "LTCUSDT-1m-2018-02.zip", [ok, late, nxt]), "LTCUSDT", stats=stats))
    assert [b.open_ms for b in got] == [t - MIN, t + MIN] and stats == {"LTCUSDT:dropped_late_close": 1}
    worse = f"{t},1,1,1,1,1,{t + MIN + 2_500},1,3,0.5,0.5,0"                   # 2.5 s late: still a defect
    with pytest.raises(ValueError):
        list(loader.iter_klines(z(tmp_path / "x" / "LTCUSDT-1m-2018-02.zip", [worse]), "LTCUSDT"))
