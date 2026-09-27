"""Tests for the trend_real plugin (T3.3c): scripts/trend_follower.py as it trades, ported read-only.

Line references are to scripts/trend_follower.py (see docs/loop/designs/T3.3c.md). The plugin is driven
directly through the real Context / BarStore / ReplayClock with hand-built daily and 1m bars, so each rule
is checked in isolation; the engine ordering (sells before buys) is checked through LoopCore.
"""
from __future__ import annotations

import pytest

from trading.replay.bars import Bar
from trading.replay.broker import CostModel, PaperAccount
from trading.replay.context import BarStore, Context, ReplayClock
from trading.replay.engine import LoopCore
from trading.replay.plugins.trend_real import REAL_UNIVERSE, TrendReal
from trading.replay.strategy import Signal

DAY, H4, M1, OFF = 86_400_000, 14_400_000, 60_000, 600_000
T0 = 1_609_459_200_000            # 2021-01-01 00:00 UTC
BTC = "BTCUSDT"


def _bar(sym, iv, end, close, step):
    return Bar(sym, iv, end - step, end, close, close, close, close, 1.0, close, 1, 0.5)


class _Acct:
    def __init__(self):
        self.positions = {}


class Harness:
    """Daily history for each symbol ending the day before `today`; checks at today + 10 min + k * 4 h."""

    def __init__(self, strat, daily: dict[str, list[float]], days_before=0):
        self.s, self.store, self.clock, self.acct = strat, BarStore(), ReplayClock(), _Acct()
        self.ctx = Context(self.store, self.clock, self.acct)
        self.today = T0 + 400 * DAY
        for sym, closes in daily.items():
            n = len(closes)
            for i, c in enumerate(closes):
                self.store.append(_bar(sym, "1d", self.today - (n - 1 - i) * DAY, c, DAY))

    def check_at(self, t, prices: dict[str, float]) -> list[Signal]:
        for sym in sorted(prices):
            self.store.append(_bar(sym, "1m", t, prices[sym], M1))
        self.clock.advance(t)
        return self.s.on_bar(_bar(BTC, "1m", t, prices[BTC], M1), self.ctx)

    def check(self, k, prices):
        return self.check_at(self.today + OFF + k * H4, prices)

    def fill(self, sigs):
        for g in sigs:
            if g.action == "open":
                self.acct.positions[g.symbol] = {"qty": 1.0, "entry": g.ref_price}
            else:
                self.acct.positions.pop(g.symbol, None)


BULL_BTC = [100.0] * 250           # BTC flat at 100: a current price of 110 is above SMA200
FLAT = [100.0] * 120


def _opens(sigs):
    return sorted(g.symbol for g in sigs if g.action == "open")


def _closes(sigs):
    return {g.symbol: g.reason for g in sigs if g.action == "close"}


def test_universe_is_the_15_real_symbols():
    assert REAL_UNIVERSE == ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT", "LINKUSDT",
                             "DOGEUSDT", "AVAXUSDT", "NEARUSDT", "SUIUSDT", "ARBUSDT", "DOTUSDT", "LTCUSDT",
                             "ATOMUSDT")


def test_params_carry_every_rule_constant():
    p = TrendReal(sma=100, hard_stop=None, universe=(BTC, "AAAUSDT")).params
    assert p == {"sma": 100, "btc_sma": 200, "hard_stop": None, "max_pos": 3, "min_bars": 60, "btc_min_bars": 205,
                 "mom_days": 30, "max_daily_bars": 300, "check_every_h": 4, "check_offset_min": 10,
                 "btc": BTC, "universe": [BTC, "AAAUSDT"]}


def test_checks_only_at_minute_10_of_every_4th_hour():
    s = TrendReal(universe=(BTC, "AAAUSDT"))
    h = Harness(s, {BTC: BULL_BTC, "AAAUSDT": FLAT})
    px = {BTC: 110.0, "AAAUSDT": 110.0}
    assert h.check_at(h.today + OFF - M1, px) == [] and s.checks == 0          # 00:09: not a check
    assert _opens(h.check_at(h.today + OFF, px)) == ["AAAUSDT", BTC]            # 00:10: check
    assert s.checks == 1
    h.check_at(h.today + OFF + M1, px)                                         # 00:11: not a check
    h.check_at(h.today + OFF + H4 - M1, px)                                    # 04:09: not a check
    assert s.checks == 1
    h.check_at(h.today + OFF + H4, px)                                         # 04:10: check
    assert s.checks == 2


def test_a_missed_check_instant_runs_on_the_next_btc_bar():
    s = TrendReal(universe=(BTC, "AAAUSDT"))
    h = Harness(s, {BTC: BULL_BTC, "AAAUSDT": FLAT})
    h.check_at(h.today + OFF, {BTC: 110.0, "AAAUSDT": 110.0})
    h.check_at(h.today + OFF + H4 + 5 * M1, {BTC: 110.0, "AAAUSDT": 110.0})   # 04:10 bar missing, 04:15 present
    assert s.checks == 2


def test_uses_the_forming_daily_candle_as_the_current_price():
    # completed closes all 100: on completed bars alone the coin sits exactly on its SMA (no entry);
    # the forming candle at 101 puts it above (trend_follower.py:213-214, closes[-1] is the live price)
    s = TrendReal(universe=(BTC, "AAAUSDT"))
    h = Harness(s, {BTC: BULL_BTC, "AAAUSDT": FLAT})
    assert "AAAUSDT" in _opens(h.check(0, {BTC: 110.0, "AAAUSDT": 101.0}))


def test_entry_needs_btc_above_its_sma200_not_sma_n():
    s = TrendReal(universe=(BTC, "AAAUSDT"))
    # BTC above its SMA50 (last 50 days at 80) but below its SMA200 (older days at 150): no entry
    btc = [150.0] * 200 + [80.0] * 50
    h = Harness(s, {BTC: btc, "AAAUSDT": FLAT})
    assert _opens(h.check(0, {BTC: 90.0, "AAAUSDT": 110.0})) == []


def test_btc_needs_205_bars_for_the_regime():
    s = TrendReal(universe=(BTC, "AAAUSDT"))
    h = Harness(s, {BTC: [100.0] * 203, "AAAUSDT": FLAT})              # 203 completed + forming = 204
    assert _opens(h.check(0, {BTC: 110.0, "AAAUSDT": 110.0})) == []
    s2 = TrendReal(universe=(BTC, "AAAUSDT"))
    h2 = Harness(s2, {BTC: [100.0] * 204, "AAAUSDT": FLAT})            # 205
    assert "AAAUSDT" in _opens(h2.check(0, {BTC: 110.0, "AAAUSDT": 110.0}))


def test_btc_filter_breaking_does_not_exit_but_coin_trend_break_does():
    s = TrendReal(universe=(BTC, "AAAUSDT"))
    h = Harness(s, {BTC: BULL_BTC, "AAAUSDT": FLAT})
    h.fill(h.check(0, {BTC: 110.0, "AAAUSDT": 110.0}))
    assert "AAAUSDT" in h.acct.positions
    # BTC falls below its SMA200: the regime is off, but AAA is still above its own SMA50, so AAA stays
    # (only BTC-the-coin, held too, exits on its own trend break)
    # (drops kept under 15% so the reason is the trend, not the hard stop, which wins when both hit: :235)
    assert _closes(h.check(1, {BTC: 99.5, "AAAUSDT": 110.0})) == {BTC: "trend-broke"}
    assert _closes(h.check(2, {BTC: 99.5, "AAAUSDT": 99.0})) == {"AAAUSDT": "trend-broke"}


def test_hard_stop_from_the_paper_entry_price():
    daily = {BTC: BULL_BTC, "AAAUSDT": [80.0] * 120}
    s = TrendReal(universe=(BTC, "AAAUSDT"))
    h = Harness(s, daily)
    h.fill(h.check(0, {BTC: 110.0, "AAAUSDT": 110.0}))                  # paper entry 110
    assert _closes(h.check(1, {BTC: 110.0, "AAAUSDT": 94.0})) == {}      # -14.5%: above SMA, no stop yet
    assert _closes(h.check(2, {BTC: 110.0, "AAAUSDT": 93.0})) == {"AAAUSDT": "HARD-STOP"}   # -15.5%

    s_off = TrendReal(hard_stop=None, universe=(BTC, "AAAUSDT"))
    h_off = Harness(s_off, daily)
    h_off.fill(h_off.check(0, {BTC: 110.0, "AAAUSDT": 110.0}))
    assert _closes(h_off.check(1, {BTC: 110.0, "AAAUSDT": 93.0})) == {}


def test_max_pos_ranked_by_30d_momentum_and_entries_are_edge_triggered():
    coins = ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT"]
    # 30 days ago each coin was at a different level; all are flat at 100 over the last 29 completed days
    start = {"AAAUSDT": 50.0, "BBBUSDT": 90.0, "CCCUSDT": 60.0, "DDDUSDT": 95.0, "EEEUSDT": 70.0}
    daily = {BTC: BULL_BTC}
    for c in coins:
        daily[c] = [100.0] * 90 + [start[c]] + [100.0] * 29
    s = TrendReal(universe=(BTC, *coins))
    h = Harness(s, daily)
    px = {BTC: 99.0, **{c: 101.0 for c in coins}}
    sigs = h.check(0, px)
    assert _opens(sigs) == []                                            # BTC 99 < SMA200 100: no regime
    px[BTC] = 100.5
    sigs = h.check(1, px)
    assert _opens(sigs) == ["AAAUSDT", "CCCUSDT", "EEEUSDT"]             # highest 30d momentum: from 50, 60, 70
    h.fill(sigs)
    # AAA breaks its trend; BBB and DDD are still paper-held (their entry fired while slots were full), so the
    # freed slot is NOT given to them: entries fire only on a paper not-held -> held edge (trend_follower.py:226)
    sigs = h.check(2, {**px, "AAAUSDT": 90.0})
    assert _closes(sigs) == {"AAAUSDT": "trend-broke"} and _opens(sigs) == []


def test_slot_freed_this_check_is_refilled_this_check():
    coins = ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"]
    # BTC at 99: below its own SMA50 (~100, so BTC-the-coin is not a candidate) but above its SMA200 (~75)
    daily = {BTC: [50.0] * 150 + [100.0] * 100, **{c: FLAT for c in coins}}
    s = TrendReal(universe=(BTC, *coins))
    h = Harness(s, daily)
    h.fill(h.check(0, {BTC: 99.0, "AAAUSDT": 110.0, "BBBUSDT": 110.0, "CCCUSDT": 110.0, "DDDUSDT": 99.0}))
    assert sorted(h.acct.positions) == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    sigs = h.check(1, {BTC: 110.0, "AAAUSDT": 99.0, "BBBUSDT": 110.0, "CCCUSDT": 110.0, "DDDUSDT": 110.0})
    # BTC (coin) and DDD enter paper now; AAA exits and frees one slot; one of the two is bought (by momentum)
    assert _closes(sigs) == {"AAAUSDT": "trend-broke"}
    assert len(_opens(sigs)) == 1


def test_min_bars_is_max_60_and_sma_n():
    s50 = TrendReal(sma=50, universe=(BTC, "AAAUSDT"))
    h50 = Harness(s50, {BTC: BULL_BTC, "AAAUSDT": [100.0] * 79})        # 80 bars with the forming one
    assert "AAAUSDT" in _opens(h50.check(0, {BTC: 110.0, "AAAUSDT": 110.0}))
    s100 = TrendReal(sma=100, universe=(BTC, "AAAUSDT"))
    h100 = Harness(s100, {BTC: BULL_BTC, "AAAUSDT": [100.0] * 79})
    assert "AAAUSDT" not in _opens(h100.check(0, {BTC: 110.0, "AAAUSDT": 110.0}))
    s_young = TrendReal(sma=50, universe=(BTC, "AAAUSDT"))
    h_young = Harness(s_young, {BTC: BULL_BTC, "AAAUSDT": [100.0] * 58})  # 59 bars < 60 (trend_follower.py:211)
    assert "AAAUSDT" not in _opens(h_young.check(0, {BTC: 110.0, "AAAUSDT": 110.0}))


def test_coin_without_a_bar_today_is_skipped():
    s = TrendReal(universe=(BTC, "AAAUSDT"))
    h = Harness(s, {BTC: BULL_BTC, "AAAUSDT": FLAT})
    h.store.append(_bar("AAAUSDT", "1m", h.today, 110.0, M1))           # last AAA print is yesterday's close
    t = h.today + OFF
    h.store.append(_bar(BTC, "1m", t, 110.0, M1))
    h.clock.advance(t)
    assert _opens(s.on_bar(_bar(BTC, "1m", t, 110.0, M1), h.ctx)) == [BTC]


def test_no_btc_history_means_no_entries():
    s = TrendReal(universe=(BTC, "AAAUSDT"))
    h = Harness(s, {"AAAUSDT": FLAT})
    assert h.check(0, {BTC: 110.0, "AAAUSDT": 110.0}) == []


# ----- engine: at one instant, sells fill before buys (trend_follower.py:208-308) ----------------
class _Scripted:
    name, params, intervals = "scripted", {}, ("1m",)

    def __init__(self, plan):
        self.plan = plan

    def on_bar(self, bar, ctx):
        if bar.symbol != "ZZZ":
            return []
        return [Signal("scripted", sym, act, ctx.now_ms, "t", bar.close, "1m") for sym, act in self.plan.get(bar.end_ms, [])]


def test_engine_fills_sells_before_buys_at_the_same_instant():
    t1, t2 = T0 + M1, T0 + 2 * M1
    # one slot: hold BBB, then at t2 sell BBB and buy AAA. AAA sorts first, so a symbol-ordered fill loop
    # would refuse AAA ("no free slot") although the same-instant sale frees it.
    strat = _Scripted({t1: [("BBB", "open")], t2: [("BBB", "close"), ("AAA", "open")]})
    acct = PaperAccount(100.0, 1, CostModel(0.0, 0.0, 0.0), name="net")
    core = LoopCore(["AAA", "BBB", "ZZZ"], [strat], {"net": acct})

    def bars(end):
        return [_bar(s, "1m", end, 10.0, M1) for s in ("AAA", "BBB", "ZZZ")]

    core.step(t1, bars(t1), [Bar(s, "1m", t1, t1 + M1, 10.0, 10, 10, 10, 1, 10, 1, 0.5) for s in ("AAA", "BBB", "ZZZ")])
    assert list(acct.positions) == ["BBB"]
    core.step(t2, bars(t2), [Bar(s, "1m", t2, t2 + M1, 10.0, 10, 10, 10, 1, 10, 1, 0.5) for s in ("AAA", "BBB", "ZZZ")])
    assert list(acct.positions) == ["AAA"]
    assert not [e for e in acct.events if e["type"] == "skip"]


# ----- the replay CLI runs trend_real ---------------------------------------------------------------
def _history(root, sym, months):
    import zipfile
    from datetime import datetime, timezone
    for i, ym in enumerate(months):
        y, m = map(int, ym.split("-"))
        d0 = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)
        rows = []
        for d in range(28):
            px = 100 + i * 10 + d
            for k in (0, 610, 1439):
                o = d0 + d * DAY + k * M1
                rows.append(f"{o},{px},{px},{px},{px},1,{o + M1 - 1},{px},3,0.5,{px / 2},0")
        p = root / "klines_1m" / sym / f"{sym}-1m-{ym}.zip"
        p.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(p.name.replace(".zip", ".csv"), "\n".join(rows) + "\n")


def _args(root, out, syms):
    return ["--history-root", str(root), "--symbols", syms, "--warmup-start", "2021-01", "--trade-start",
            "2021-02", "--is-oos-split", "2021-03", "--wf-fit", "1", "--wf-trade", "1", "--out", str(out),
            "--strategy", "trend_real", "--broker", "paper", "--fill-cap-usd", "none"]


def test_replay_cli_runs_trend_real_and_logs_its_params_as_the_trial(tmp_path):
    import json
    from trading.replay import run
    months = [f"2021-{m:02d}" for m in range(1, 10)]
    for s in ("BTCUSDT", "ETHUSDT"):
        _history(tmp_path / "h", s, months)
    summ = run.main(_args(tmp_path / "h", tmp_path / "o", "BTCUSDT,ETHUSDT") + ["--sma", "100", "--hard-stop", "none"])
    assert summ["strategy"] == "trend_real" and summ["slots"] == 3
    assert summ["params"]["sma"] == 100 and summ["params"]["hard_stop"] is None
    assert summ["params"]["universe"] == ["BTCUSDT", "ETHUSDT"]
    assert "sharpe_daily_ann" in summ["accounts"]["net"]["periods"]["OOS"]
    assert any("trend_follower.py" in c for c in summ["caveats"])
    trial = json.loads((tmp_path / "o" / "trials.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert trial["strategy"] == "trend_real" and trial["params"] == summ["params"] and trial["broker"] == "paper"


def test_replay_cli_trend_real_refuses_an_unmeasured_symbol(tmp_path):
    from trading.paper.spreads import UnknownSpreadError
    from trading.replay import run
    months = [f"2021-{m:02d}" for m in range(1, 10)]
    for s in ("BTCUSDT", "FOOUSDT"):
        _history(tmp_path / "h", s, months)
    with pytest.raises(UnknownSpreadError):
        run.main(_args(tmp_path / "h", tmp_path / "o", "BTCUSDT,FOOUSDT"))


def test_trend_real_needs_the_benchmark_in_the_universe(tmp_path):
    from trading.replay import run
    months = [f"2021-{m:02d}" for m in range(1, 10)]
    _history(tmp_path / "h", "ETHUSDT", months)
    with pytest.raises(SystemExit):
        run.main(_args(tmp_path / "h", tmp_path / "o", "ETHUSDT"))
