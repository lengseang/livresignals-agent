"""P2a shadow scanner: isolated books, hash chain, gaps, errors, daily marks, scorer (P2-shadow-scanner.md tests)."""
from __future__ import annotations

import heapq
import json

from test_replay import daily_minutes, ms

from trading.replay.strategy import Signal
from trading.scanner.bars import ticks_from_bar
from trading.scanner.registry import PluginInfo, Registry
from trading.shadow import chain as C
from trading.shadow.fleet import ShadowFleet
from trading.shadow.score import score

D0 = ms("2021-03-01T00:00:00")


class Flip:
    """Opens its coin on the first daily close it sees, closes on the next one, repeats."""
    intervals = ("1d",)

    def __init__(self, coin: str = "BTCUSDT", name: str = "flip"):
        self.name, self.params, self.coin = name, {"coin": coin}, coin

    def on_bar(self, bar, ctx):
        if bar.symbol != self.coin or bar.interval != "1d":
            return []
        action = "close" if ctx.position(self.coin) is not None else "open"
        return [Signal(self.name, self.coin, action, ctx.now_ms, "test", bar.close, "1d")]


class Boom(Flip):
    def __init__(self):
        super().__init__("BTCUSDT", "boom")
        self.n = 0

    def on_bar(self, bar, ctx):
        self.n += bar.interval == "1d"
        if self.n >= 3:
            raise RuntimeError("plugin bug")
        return super().on_bar(bar, ctx)


def registry() -> Registry:
    r = Registry()
    r.register(PluginInfo(name="flip_btc", family="test", factory=lambda: Flip("BTCUSDT", "flip_btc")))
    r.register(PluginInfo(name="flip_eth", family="test", factory=lambda: Flip("ETHUSDT", "flip_eth")))
    r.register(PluginInfo(name="boom", family="test", factory=Boom))
    return r


def ticks(days: int = 6):
    bars = {s: daily_minutes(s, D0, [100.0 + i for i in range(days)], per_day=2) for s in ("BTCUSDT", "ETHUSDT")}
    streams = [ticks_from_bar(b) for s in bars for b in bars[s]]
    return list(heapq.merge(*streams, key=lambda t: (t.ts_exchange_ms, t.symbol)))


def run(tmp_path, names, days=6, gap=None):
    fleet = ShadowFleet(["BTCUSDT", "ETHUSDT"], [{"lane": "candidate", "strategy": n, "slots": 1} for n in names],
                        tmp_path, forward_start_ms=D0, registry=registry(), grace_ms=None)
    for i, t in enumerate(ticks(days)):
        if gap and i == gap[0]:
            fleet.gap(*gap[1:])
        fleet.on_tick(t)
    fleet.finish()
    return fleet


def records(tmp_path, lane, name):
    out = []
    for f in sorted((tmp_path / lane / name).glob("*.jsonl")):
        out += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    return out


def test_candidates_trade_isolated_books(tmp_path):
    fleet = run(tmp_path, ["flip_btc", "flip_eth"])
    a, b = records(tmp_path, "candidate", "flip_btc"), records(tmp_path, "candidate", "flip_eth")
    assert {r["data"]["symbol"] for r in a if r["type"] == "signal"} == {"BTCUSDT"}
    assert {r["data"]["symbol"] for r in b if r["type"] == "signal"} == {"ETHUSDT"}
    books = {bk.name: bk for bk in fleet.books}
    assert all(s == "BTCUSDT" for s in books["flip_btc"].account.positions)
    assert all(t["symbol"] == "ETHUSDT" for t in books["flip_eth"].account.trades)


def test_the_chain_verifies_links_across_days_and_detects_any_edit(tmp_path):
    run(tmp_path, ["flip_btc"])
    d = tmp_path / "candidate" / "flip_btc"
    days = sorted(d.glob("*.jsonl"))
    assert len(days) >= 3 and C.verify(d)["ok"]
    first_of_day2 = json.loads(days[1].read_text(encoding="utf-8").splitlines()[0])
    last_of_day1 = json.loads(days[0].read_text(encoding="utf-8").splitlines()[-1])
    assert first_of_day2["prev_hash"] == last_of_day1["hash"]
    text = days[1].read_text(encoding="utf-8")
    days[1].write_text(text.replace('"ref_price":', '"ref_price":1', 1), encoding="utf-8")   # one byte-level edit
    assert not C.verify(d)["ok"]


def test_signals_fills_and_daily_marks_are_recorded(tmp_path):
    run(tmp_path, ["flip_btc"])
    types = [r["type"] for r in records(tmp_path, "candidate", "flip_btc")]
    assert "signal" in types and "shadow_fill" in types and types.count("shadow_mark") >= 3
    first_mark = next(r for r in records(tmp_path, "candidate", "flip_btc") if r["type"] == "shadow_mark")
    assert first_mark["data"]["day"] == "2021-03-01"                   # never a 1970 mark from the idle clock


def test_a_crashing_candidate_is_stopped_and_the_others_continue(tmp_path):
    run(tmp_path, ["boom", "flip_btc"])
    boom = records(tmp_path, "candidate", "boom")
    assert any(r["type"] == "error" and "plugin bug" in r["data"]["error"] for r in boom)
    flips = [r for r in records(tmp_path, "candidate", "flip_btc") if r["type"] == "signal"]
    err_t = next(r["ts_event_ms"] for r in boom if r["type"] == "error")
    assert any(r["ts_event_ms"] > err_t for r in flips) or len(flips) >= 4
    assert score(tmp_path)["books"][0]["status"] == "STOPPED"


def test_a_gap_is_recorded_never_backfilled_and_excluded_from_the_score(tmp_path):
    n = len(ticks())
    run(tmp_path, ["flip_btc"], gap=(n // 2, D0 + 2 * 86_400_000 + 1, D0 + 2 * 86_400_000 + 2, "pc slept"))
    recs = records(tmp_path, "candidate", "flip_btc")
    assert any(r["type"] == "gap" and r["data"]["reason"] == "pc slept" for r in recs)
    card = score(tmp_path)["books"][0]
    assert card["chain"]["ok"] and "2021-03-03" in card["gap_days"] and card["status"] == "ACCUMULATING"


def test_a_tampered_chain_is_unverified_and_reports_no_numbers(tmp_path):
    run(tmp_path, ["flip_btc"])
    f = sorted((tmp_path / "candidate" / "flip_btc").glob("*.jsonl"))[0]
    lines = f.read_text(encoding="utf-8").splitlines()
    f.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")        # drop the first record
    card = score(tmp_path)["books"][0]
    assert card["status"] == "UNVERIFIED" and "return" not in card
