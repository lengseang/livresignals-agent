"""V2-7: the paper book, its context provider, and the paper-mode pipeline. Simulation only."""
import ast
import pathlib

import pytest

from trading.replay.bars import Bar
from trading.replay.broker import CostModel
from trading.shadow.chain import ShadowChain, verify
from trading.v2.adapter import JevAdapter
from trading.v2.contracts import ExecutionCommand, StrategyRef
from trading.v2.context import PaperContextProvider
from trading.v2.fallback import StaticSource
from trading.v2.paper import PaperBook
from trading.v2.policy import ExitParams
from trading.v2.risk import ExchangeHealth, RiskLimits
from trading.v2.shadow import ShadowPipeline
from trading.v2.workflow import load_active
from trading.v2.contracts import FALLBACK_STRATEGIES

from test_v2_contracts import execution_command, mutated
from test_v2_features import T0
from test_v2_shadow import Client, FollowBook, LONG_RAW, make_bars

SYM = "BTCUSDT"
M = 60_000
ZERO = CostModel(fee_rate=0.0, spread_bps=0.0, slippage_bps=0.0)
FEE = CostModel(fee_rate=0.001, spread_bps=0.0, slippage_bps=0.0)


def b(i, o, h=None, l=None, c=None, t0=T0):
    h = max(o, c or o) if h is None else h
    l = min(o, c or o) if l is None else l
    c = o if c is None else c
    s = t0 + i * M
    return Bar(SYM, "1m", s, s + M, o, h, l, c, 10.0, c * 10.0, 5, 5.0)


def cmd(key="1", **over):
    d = mutated(execution_command, size=1.0, stop_price=95.0, idempotency_key=f"tp_01J8ZABCDEFGH-v{key}")
    d.update(over)
    return ExecutionCommand.from_dict(d)


def close_cmd(size=1.0, key="9", side="SELL", position_side="LONG"):
    return cmd(key=key, side=side, position_side=position_side, reduce_only=True, stop_price=None, size=size)


def short_cmd(key="3", stop=105.0, size=1.0):
    return cmd(key=key, side="SELL", position_side="SHORT", stop_price=stop, size=size)


def book(costs=ZERO, equity=1000.0, sink=None):
    return PaperBook(equity, costs, sink)


# ------------------------------------------------------------ fills and timing

def test_entry_fills_at_the_next_bar_open_with_costs_and_the_stop_attached():
    bk = book(CostModel(fee_rate=0.001, spread_bps=2.0, slippage_bps=1.0), equity=1000.0)
    assert bk.submit(cmd(size=2.0), ts_ms=T0 + M) == "queued"
    bk.on_bar(b(0, 100.0))                       # opens at T0 < submit time: must NOT fill
    assert not bk.positions and len(bk.pending) == 1
    bk.on_bar(b(1, 100.0, 101, 99.5, 100.5))     # opens at T0+M == submit time: fills at this open
    p = bk.positions[SYM]
    px = 100.0 * (1 + (1.0 + 1.0) / 1e4)         # half spread 1bp + slippage 1bp
    assert (p.side, p.qty, p.stop) == ("LONG", 2.0, 95.0) and p.entry == pytest.approx(px)
    assert bk.cash == pytest.approx(1000.0 - 2.0 * px - 0.001 * 2.0 * px)
    assert bk.pending == [] and bk.stats["opened"] == 1


def test_a_command_never_fills_before_it_was_submitted():
    bk = book()
    bk.submit(cmd(), ts_ms=T0 + 5 * M)
    for i in range(5):
        bk.on_bar(b(i, 100.0))
    assert not bk.positions
    bk.on_bar(b(5, 100.0))
    assert SYM in bk.positions


def test_idempotency_key_makes_a_retry_a_no_op_even_after_the_fill():
    bk = book()
    assert bk.submit(cmd(), T0) == "queued"
    assert bk.submit(cmd(), T0) == "duplicate_ignored"
    bk.on_bar(b(0, 100.0))
    assert bk.submit(cmd(), T0 + M) == "duplicate_ignored"
    bk.on_bar(b(1, 100.0))
    assert bk.positions[SYM].qty == 1.0 and bk.stats["opened"] == 1 and bk.stats["duplicate_ignored"] == 2


def test_second_entry_while_positioned_is_rejected():
    bk = book()
    bk.submit(cmd("1"), T0); bk.on_bar(b(0, 100.0))
    bk.submit(cmd("2"), T0 + M); bk.on_bar(b(1, 100.0))
    assert bk.positions[SYM].qty == 1.0 and bk.stats["rejected:position_exists"] == 1


def test_insufficient_cash_is_rejected_not_partially_filled():
    bk = book(equity=50.0)
    bk.submit(cmd(size=1.0), T0); bk.on_bar(b(0, 100.0))
    assert not bk.positions and bk.cash == 50.0 and bk.stats["rejected:insufficient_cash"] == 1


def test_limit_orders_are_rejected_not_approximated():
    bk = book()
    lim = cmd(type="LIMIT", limit_price=99.0)
    assert bk.submit(lim, T0) == "rejected:limit_orders_not_supported_in_paper_v1"
    bk.on_bar(b(0, 100.0))
    assert not bk.positions and not bk.pending


def test_only_closed_one_minute_bars_are_accepted():
    with pytest.raises(ValueError):
        book().on_bar(Bar(SYM, "1h", 0, 1, 1, 1, 1, 1, 1, 1, 1, 1))


# ------------------------------------------------------------ exact P&L

def test_round_trip_pnl_matches_hand_computation_with_both_fees():
    bk = book(FEE)
    bk.submit(cmd(), T0); bk.on_bar(b(0, 100.0))
    bk.submit(close_cmd(), T0 + M); bk.on_bar(b(1, 110.0, 111, 109, 110))
    t = bk.trades[0]
    assert t["pnl"] == pytest.approx(10.0 - 0.11 - 0.10) and t["reason"] == "exit" and not bk.positions
    assert bk.cash == pytest.approx(1000.0 + t["pnl"]) and bk.equity() == pytest.approx(bk.cash)


def test_partial_close_apportions_the_entry_fee_and_conserves_total_pnl():
    full = book(FEE); half = book(FEE)
    for bk in (full, half):
        bk.submit(cmd(size=2.0), T0); bk.on_bar(b(0, 100.0))
    full.submit(close_cmd(2.0), T0 + M); full.on_bar(b(1, 110.0))
    half.submit(close_cmd(1.0, key="8"), T0 + M); half.on_bar(b(1, 110.0))
    assert half.positions[SYM].qty == 1.0
    half.submit(close_cmd(1.0, key="9"), T0 + 2 * M); half.on_bar(b(2, 110.0))
    assert sum(t["pnl"] for t in half.trades) == pytest.approx(full.trades[0]["pnl"])
    assert half.cash == pytest.approx(full.cash)
    # per-trade attribution: each half carries HALF of the 0.20 entry fee (a total-only check misses this)
    assert [round(t["pnl"], 6) for t in half.trades] == [round(10.0 - 0.11 - 0.10, 6)] * 2
    assert [round(t["fees"], 6) for t in half.trades] == [0.21, 0.21]


def test_reduce_only_never_closes_more_than_held_or_the_wrong_side():
    bk = book()
    bk.submit(cmd(size=1.0), T0); bk.on_bar(b(0, 100.0))
    bk.submit(close_cmd(5.0), T0 + M); bk.on_bar(b(1, 100.0))
    assert bk.trades[0]["qty"] == 1.0 and not bk.positions
    bk.submit(close_cmd(1.0, key="7"), T0 + 2 * M); bk.on_bar(b(2, 100.0))
    assert bk.stats["rejected:no_matching_position"] == 1


def test_short_round_trip_is_the_mirror_image():
    bk = book(FEE)
    bk.submit(short_cmd(), T0); bk.on_bar(b(0, 100.0))
    assert bk.positions[SYM].side == "SHORT" and bk.cash == pytest.approx(1000.0 + 100.0 - 0.1)
    bk.submit(close_cmd(1.0, key="5", side="BUY", position_side="SHORT"), T0 + M); bk.on_bar(b(1, 90.0, 91, 89, 90))
    assert bk.trades[0]["pnl"] == pytest.approx(10.0 - 0.09 - 0.10)
    assert not bk.positions and bk.equity() == pytest.approx(1000.0 + bk.trades[0]["pnl"])


def test_equity_is_marked_at_each_close():
    bk = book()
    bk.submit(cmd(size=2.0), T0); bk.on_bar(b(0, 100.0, 100, 99, 99.0))
    assert bk.equity() == pytest.approx(1000.0 - 2.0)          # 2 units marked at 99
    bk.on_bar(b(1, 99.0, 106, 99, 105.0))
    assert bk.equity() == pytest.approx(1000.0 + 10.0) and bk.open_notional() == pytest.approx(210.0)


def test_random_trade_sequence_conserves_money():
    x, bk, key, t = 12345, book(FEE, equity=10_000.0), 0, 0
    px = 100.0
    for _ in range(150):
        x = (1103515245 * x + 12345) % 2 ** 31
        px *= 1 + ((x / 2 ** 31) - 0.5) * 0.02
        key += 1
        if SYM in bk.positions:
            bk.submit(close_cmd(5.0, key=str(key)), T0 + t * M)
        else:
            bk.submit(cmd(str(key), size=5.0, stop_price=px * 0.5), T0 + t * M)
        bk.on_bar(b(t, px, px * 1.01, px * 0.99, px))
        t += 1
    if SYM in bk.positions:
        bk.submit(close_cmd(5.0, key="end"), T0 + t * M); bk.on_bar(b(t, px)); t += 1
    assert not bk.positions and len(bk.trades) > 20
    assert bk.cash == pytest.approx(10_000.0 + sum(tr["pnl"] for tr in bk.trades), abs=1e-6)
    assert bk.equity() == pytest.approx(bk.cash)


# ------------------------------------------------------------ the protective stop

def test_long_stop_fills_at_the_stop_price_when_touched():
    bk = book()
    bk.submit(cmd(size=1.0, stop_price=95.0), T0); bk.on_bar(b(0, 100.0, 100.5, 99.9, 100.2))
    bk.on_bar(b(1, 100.0, 100.2, 94.0, 96.0))
    t = bk.trades[0]
    assert t["reason"] == "stop" and t["exit"] == 95.0 and t["pnl"] == pytest.approx(-5.0) and not bk.positions


def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop():
    bk = book()
    bk.submit(cmd(size=1.0, stop_price=95.0), T0); bk.on_bar(b(0, 100.0))
    bk.on_bar(b(1, 90.0, 91.0, 89.0, 90.0))
    assert bk.trades[0]["exit"] == 90.0 and bk.trades[0]["pnl"] == pytest.approx(-10.0)


def test_the_stop_is_also_checked_on_the_entry_bar():
    bk = book()
    bk.submit(cmd(size=1.0, stop_price=99.0), T0); bk.on_bar(b(0, 100.0, 100.5, 98.0, 98.5))
    assert bk.trades[0]["reason"] == "stop" and not bk.positions


def test_untouched_stop_leaves_the_position_open():
    bk = book()
    bk.submit(cmd(size=1.0, stop_price=95.0), T0); bk.on_bar(b(0, 100.0))
    bk.on_bar(b(1, 100.0, 101, 95.01, 100))
    assert SYM in bk.positions and not bk.trades


def test_short_stop_and_gap():
    bk = book()
    bk.submit(short_cmd(stop=105.0), T0); bk.on_bar(b(0, 100.0))
    bk.on_bar(b(1, 100.0, 106.0, 99.0, 104.0))
    assert bk.trades[0]["exit"] == 105.0 and bk.trades[0]["pnl"] == pytest.approx(-5.0)
    bk.submit(short_cmd(key="4", stop=105.0), T0 + 2 * M); bk.on_bar(b(2, 100.0))
    bk.on_bar(b(3, 110.0, 111.0, 109.0, 110.0))
    assert bk.trades[1]["exit"] == 110.0


def test_stop_slippage_is_paid():
    bk = book(CostModel(0.0, 0.0, 10.0))        # 10 bps slippage
    bk.submit(cmd(size=1.0, stop_price=95.0), T0); bk.on_bar(b(0, 100.0))
    bk.on_bar(b(1, 100.0, 100, 94, 96))
    assert bk.trades[0]["exit"] == pytest.approx(95.0 * (1 - 10 / 1e4))


# ------------------------------------------------------------ day / week / high-water mark

def test_day_week_and_hwm_roll_correctly_including_early_in_a_month():
    # 2026-10-02 (Fri) 23:58 UTC: the case that breaks the legacy week helper
    fri = int(__import__("datetime").datetime(2026, 10, 2, 23, 58, tzinfo=__import__("datetime").timezone.utc).timestamp() * 1000)
    bk = book(equity=1000.0)
    bk.submit(cmd(size=1.0, stop_price=1.0), fri - M); bk.on_bar(b(0, 100.0, 100, 100, 100.0, t0=fri))
    bk.on_bar(b(1, 100.0, 120, 100, 120.0, t0=fri))                 # +20, still Friday
    assert bk.day_start_equity == 1000.0 and bk.high_water_mark == pytest.approx(1020.0)
    bk.on_bar(b(2, 120.0, 120, 110, 110.0, t0=fri))                 # opens at 00:00 Saturday: new day
    assert bk.day_start_equity == pytest.approx(1020.0)             # equity at the rollover
    assert bk.week_start_equity == 1000.0                           # same Mon-Sun week
    assert bk.daily_pnl() == pytest.approx(1010.0 - 1020.0) and bk.high_water_mark == pytest.approx(1020.0)
    mon = int(__import__("datetime").datetime(2026, 10, 5, 0, 0, tzinfo=__import__("datetime").timezone.utc).timestamp() * 1000)
    bk.on_bar(b(0, 110.0, 110, 110, 110.0, t0=mon))
    assert bk.week_start_equity == pytest.approx(1010.0)


def test_consecutive_losses_and_realized_pct():
    bk = book(equity=1000.0)
    for i, exit_px in enumerate((90.0, 95.0, 120.0, 80.0, 85.0)):
        t = i * 2
        bk.submit(cmd(str(i), size=1.0, stop_price=1.0), T0 + t * M); bk.on_bar(b(t, 100.0))
        bk.submit(close_cmd(1.0, key=str(100 + i)), T0 + (t + 1) * M); bk.on_bar(b(t + 1, exit_px))
    assert [round(x["pnl"]) for x in bk.trades] == [-10, -5, 20, -20, -15]
    assert bk.consecutive_losses() == 2 and bk.realized_pnl_pct() == pytest.approx(-30.0 / 1000.0)


# ------------------------------------------------------------ log and purity

def test_every_event_is_logged_to_a_separate_hash_chained_lane_labelled_paper(tmp_path):
    sink = ShadowChain(tmp_path, "v2paper", "book", T0)
    bk = book(FEE, sink=sink)
    bk.submit(cmd(), T0); bk.submit(cmd(), T0); bk.on_bar(b(0, 100.0))
    bk.submit(close_cmd(), T0 + M); bk.on_bar(b(1, 110.0))
    day = sorted((tmp_path / "v2paper" / "book").glob("*.jsonl"))[0]
    import json
    recs = [json.loads(line) for line in day.read_text().splitlines()]
    assert [r["event"] for r in recs] == ["queued", "duplicate_ignored", "fill", "queued", "fill"]
    assert all(r["real"] is False and r["mode"] == "PAPER_V2" and "not real money" in r["label"] for r in recs)
    assert recs[-1]["action"] == "close" and recs[-1]["pnl"] == pytest.approx(9.79)
    assert verify(tmp_path / "v2paper" / "book")["ok"]


def test_paper_modules_have_no_order_path_clock_or_network():
    for mod in ("paper", "context"):
        src = pathlib.Path(f"src/trading/v2/{mod}.py").read_text()
        names = set()
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Import):
                names |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                names.add((n.module or "").split(".")[0] if (n.module or "").split(".")[0] != "trading" else n.module)
        assert not [m for m in names if m in {"socket", "requests", "httpx", "ccxt", "subprocess", "os", "time",
                                              "random", "sqlite3"} or "live_client" in m or "exchange" in m], (mod, names)
        assert ".now(" not in src and "time.time" not in src and "create_order" not in src


# ------------------------------------------------------------ context provider

STRAT = StrategyRef(name="btc_breakout_v3", version="3.2.1")
OKH = ExchangeHealth(True, "ok")


def provider(bk, cap=100.0, budget=None, health=OKH, kill=False, **kw):
    return PaperContextProvider(bk, STRAT, RiskLimits(daily_loss_cap_usd=cap), exit_params=ExitParams(0.02, 0.04),
                                daily_loss_budget_usd=budget, exchange_health=lambda: health,
                                kill_switch=lambda: kill, **kw)


def test_context_reflects_the_book_flat_and_positioned():
    bk = book(equity=10_000.0)
    c = provider(bk).context(SYM, T0)
    assert c.position.side == "none" and c.risk_state.equity == 10_000.0 and c.open_orders == ()
    assert c.day_start_equity == c.week_start_equity == c.high_water_mark == 10_000.0
    bk.submit(cmd(size=2.0, stop_price=50.0), T0)
    assert provider(bk).context(SYM, T0).open_orders[0].reduce_only is False
    bk.on_bar(b(0, 100.0, 100, 100, 110.0))
    c = provider(bk).context(SYM, T0 + M)
    assert (c.position.side, c.position.size, c.position.entry_price) == ("long", 2.0, 100.0)
    assert c.position.unrealized_pnl == pytest.approx(20.0) and c.open_notional == pytest.approx(220.0)
    assert c.risk_state.current_exposure == pytest.approx(220.0 / 10_020.0) and c.open_orders == ()


def test_context_short_position_unrealized_pnl():
    bk = book(equity=10_000.0)
    bk.submit(short_cmd(size=2.0), T0); bk.on_bar(b(0, 100.0, 100, 100, 90.0))
    c = provider(bk).context(SYM, 0)
    assert c.position.side == "short" and c.position.unrealized_pnl == pytest.approx(20.0)


@pytest.mark.parametrize("cap,pnl,expected", [(100.0, -30.0, 70.0), (None, -30.0, 470.0), (1000.0, -30.0, 470.0),
                                              (100.0, -150.0, 0.0), (100.0, 25.0, 125.0)])
def test_daily_loss_limit_remaining_is_the_tighter_of_cap_and_percent_halt(cap, pnl, expected):
    bk = book(equity=10_000.0)
    bk.cash += pnl                        # a realised daily loss/gain
    assert provider(bk, cap=cap).context(SYM, 0).risk_state.daily_loss_limit_remaining == pytest.approx(expected)


def test_daily_loss_budget_used_needs_an_owner_defined_budget():
    bk = book(equity=10_000.0); bk.cash -= 75.0
    assert provider(bk, budget=None).context(SYM, 0).daily_loss_budget_used == 0.0
    assert provider(bk, budget=50.0).context(SYM, 0).daily_loss_budget_used == pytest.approx(1.5)


def test_unknowns_pass_through_as_none_so_risk_can_fail_closed_and_legacy_is_optional():
    c = provider(book(), health=None, kill=None).context(SYM, 0)
    assert c.exchange is None and c.kill_switch_engaged is None and c.legacy_action is None
    c = provider(book(), legacy_action=lambda s, t: "LONG").context(SYM, 0)
    assert c.legacy_action == "LONG"


# ------------------------------------------------------------ the paper-mode pipeline end to end

def paper_pipeline(tmp_path, kill=False, reply=LONG_RAW):
    WF = load_active()
    adapter = JevAdapter(Client(reply), WF, StaticSource({n: "LONG" for n in FALLBACK_STRATEGIES}),
                         timeout_s=1.0, clock=lambda: 0.0)
    bk = PaperBook(10_000.0, FEE, ShadowChain(tmp_path, "v2paper", "book", T0))
    fb = FollowBook()
    pr = provider(bk, kill=kill)
    p = ShadowPipeline(adapter=adapter, provider=pr, sink=ShadowChain(tmp_path, "v2cycle", "pipeline", T0), book=fb,
                       limits=RiskLimits(daily_loss_cap_usd=100.0), paper=bk)
    fb.pipeline = p
    return p, bk


def test_approved_decision_fills_at_the_next_open_and_the_position_blocks_duplicates(tmp_path):
    p, bk = paper_pipeline(tmp_path)
    bars = make_bars()
    recs = p.run(bars)
    live = [r for r in recs if r["outcome"] != "no_snapshot"]
    assert [(r["ts_event_ms"] - T0) // M for r in live] == [255, 270, 285, 300]
    first = live[0]
    assert first["outcome"] == "would_execute" and first["mode"] == "paper" and first["paper_submit"] == "queued"
    assert first["real"] is False and "PAPER V2" in first["label"]
    nxt = next(x for x in bars if x.interval == "1m" and x.open_ms == first["ts_event_ms"])
    pos = bk.positions[SYM]
    assert pos.opened_ms == nxt.open_ms and pos.entry == pytest.approx(nxt.open)      # next open, no look-ahead
    assert pos.qty == pytest.approx(first["execution_preview"]["size"]) and pos.stop == first["execution_preview"]["stop_price"]
    assert [r["outcome"] for r in live[1:]] == ["no_proposal"] * 3
    assert [r["proposal_reason"] for r in live[1:]] == ["already_long"] * 3
    assert dict(bk.stats) == {"queued": 1, "opened": 1}                              # ONE position, ever
    s = p.summary()["paper"]
    assert s["opened"] == 1 and s["open_positions"] == 1 and s["closed_trades"] == 0
    assert s["equity"] == pytest.approx(bk.equity()) and bk.cash < 10_000.0 * 0.01      # almost fully invested
    assert verify(tmp_path / "v2paper" / "book")["ok"] and verify(tmp_path / "v2cycle" / "pipeline")["ok"]


def test_the_next_cycle_sees_the_filled_position_in_its_state(tmp_path):
    p, bk = paper_pipeline(tmp_path)
    recs = [r for r in p.run(make_bars()) if r["outcome"] != "no_snapshot"]
    assert recs[0]["state"]["position"]["side"] == "none"
    for r in recs[1:]:
        assert r["state"]["position"]["side"] == "long"
        assert r["state"]["position"]["size"] == pytest.approx(bk.positions[SYM].qty)
        assert r["state"]["risk"]["current_exposure"] > 0.9


def test_kill_switch_means_nothing_reaches_the_paper_book(tmp_path):
    p, bk = paper_pipeline(tmp_path, kill=True)
    recs = p.run(make_bars())
    assert bk.stats["queued"] == 0 and not bk.positions and bk.equity() == 10_000.0
    assert {r["outcome"] for r in recs} == {"no_snapshot", "risk_rejected"}
    assert all(r["risk"]["reject_reason"] == "kill_switch" for r in recs if r["outcome"] == "risk_rejected")


def test_a_crash_through_the_stop_closes_at_the_gap_price_and_the_loss_cap_blocks_re_entry(tmp_path):
    bars = make_bars()
    entry_after = T0 + 256 * M
    crashed = [Bar(x.symbol, x.interval, x.open_ms, x.end_ms, x.open * 0.9, x.high * 0.9, x.low * 0.9, x.close * 0.9,
                   x.volume, x.quote_volume * 0.9, x.trades, x.taker_buy_base)
               if x.interval == "1m" and x.open_ms >= entry_after + M else x for x in bars]
    p, bk = paper_pipeline(tmp_path)
    recs = [r for r in p.run(crashed) if r["outcome"] != "no_snapshot"]
    assert dict(bk.stats) == {"queued": 1, "opened": 1, "closed": 1, "stopped": 1}
    t = bk.trades[0]
    gap_bar = next(x for x in crashed if x.interval == "1m" and x.open_ms == entry_after + M)
    assert t["reason"] == "stop" and t["exit"] == pytest.approx(gap_bar.open)   # gapped: filled at the open
    assert t["exit"] < t["entry"] * 0.98 and t["pnl"] < -1000.0 and not bk.positions
    assert bk.consecutive_losses() == 1 and bk.equity() == pytest.approx(bk.cash) and bk.equity() < 9_000.0
    # flat again, but the daily loss cap (100) now blocks every new entry: risk says no, nothing queued
    assert [r["outcome"] for r in recs] == ["would_execute", "risk_rejected", "risk_rejected", "risk_rejected"]
    assert all(r["risk"]["reject_reason"] == "daily_loss_limit" for r in recs[1:])
    assert all(r["paper_submit"] is None for r in recs[1:])
