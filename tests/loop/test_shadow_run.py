"""P2c shadow runner: lanes from the cards, benchmarks, recorder catch-up filter, an end-to-end prime."""
from __future__ import annotations

import argparse
import json
import zipfile

import pytest
from test_replay import daily_minutes, ms, row

from trading.replay.broker import CostModel, PaperAccount
from trading.replay.engine import replay
from trading.scanner.registry import DEFAULT
from trading.shadow import run as R

CARDS = {"candidates": [
    {"id": "A1", "strategy": "donchian_ensemble", "family": "trend", "stage": "rejected"},
    {"id": "A2", "strategy": "ctrend_lite", "family": "cross_section", "stage": "shadow"},
    {"id": "A4", "strategy": "high_52w", "family": "cross_section", "stage": "card"},
    {"id": "X", "strategy": "no_such_plugin", "family": "trend", "stage": "validated"},
    {"id": "B", "strategy": "buy_hold_btc", "family": "benchmark", "stage": "pre_registered"}]}


def test_lanes_come_from_the_card_stages(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(CARDS), encoding="utf-8")
    got = {(x["lane"], x["strategy"]) for x in R.plan(p, ["BTCUSDT", "ETHUSDT"])}
    assert got == {("rejected", "donchian_ensemble"), ("candidate", "ctrend_lite"),
                   ("benchmark", "buy_hold_btc"), ("benchmark", "buy_hold_ew")}
    ew = next(x for x in R.plan(p, ["BTCUSDT", "ETHUSDT"]) if x["strategy"] == "buy_hold_ew")
    assert ew["params"]["universe"] == ("BTCUSDT", "ETHUSDT")          # never the N=1 default


def test_benchmarks_hold_btc_and_an_equal_weight_basket():
    d0 = ms("2021-03-01T00:00:00")
    syms = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "ADAUSDT"]
    streams = {s: daily_minutes(s, d0, [100.0] * 5, per_day=2) for s in syms}
    ew, btc = PaperAccount(1000, 4, CostModel(0, 0, 0)), PaperAccount(1000, 1, CostModel(0, 0, 0))
    replay(streams, [DEFAULT.build("buy_hold_ew", universe=tuple(syms))], {"net": ew})
    replay(streams, [DEFAULT.build("buy_hold_btc")], {"net": btc})
    assert sorted(ew.positions) == sorted(syms)
    assert all(p["spent"] == pytest.approx(250.0) for p in ew.positions.values())
    assert list(btc.positions) == ["BTCUSDT"] and btc.positions["BTCUSDT"]["spent"] == pytest.approx(1000.0)


def test_recorder_catch_up_only_takes_ticks_after_the_history(tmp_path):
    k = tmp_path / "2026-09-27" / "kline"
    k.mkdir(parents=True)
    tick = lambda t: json.dumps({"kind": "kline", "symbol": "BTCUSDT", "ts_exchange_ms": t, "ts_recv_ns": 0,
                                 "data": {}})
    (k / "07.jsonl").write_text("\n".join(tick(t) for t in (100, 200, 300)), encoding="utf-8")
    assert [t.ts_exchange_ms for t in R.recorder_ticks(tmp_path, 150)] == [200, 300]


def _month_zip(root, sym, ym, bars):
    p = root / "monthly" / "klines_1m" / sym / f"{sym}-1m-{ym}.zip"
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(p.name.replace(".zip", ".csv"), "\n".join(row(b.open_ms, b.open, b.high, b.low, b.close)
                                                              for b in bars) + "\n")


def test_build_primes_every_book_from_history_files(tmp_path):
    d0 = ms("2021-03-01T00:00:00")
    syms = ["BTCUSDT", "ETHUSDT"]
    for s in syms:
        _month_zip(tmp_path / "hist", s, "2021-03", daily_minutes(s, d0, [100.0 + i for i in range(31)], per_day=2))
    cards = tmp_path / "c.json"
    cards.write_text(json.dumps({"candidates": [
        {"id": "A3", "strategy": "tsmom_risk_managed", "family": "trend", "stage": "rejected"}]}), encoding="utf-8")
    (tmp_path / "ticks").mkdir()
    a = argparse.Namespace(root=str(tmp_path / "shadow"), history=str(tmp_path / "hist"), ticks=str(tmp_path / "ticks"),
                           cards=str(cards), symbols=",".join(syms), prime_days=40)
    fleet, rep = R.build(a, ms("2021-04-01T00:00:00"))
    assert rep["steps"] > 0 and not any(b["stopped"] for b in rep["books"])
    held = {b["strategy"]: b["positions"] for b in rep["books"]}
    assert held["buy_hold_btc"] == ["BTCUSDT"] and held["buy_hold_ew"] == syms
    tsmom = next(b for b in fleet.books if b.name == "tsmom_risk_managed")
    # its first decision needs 31 closes, so it lands on the last history close: the order waits for the first
    # live open, as it would live (never filled at a price from before the forward start)
    assert "BTCUSDT" in {sig.symbol for sig in tsmom.account.pending} and not tsmom.account.positions
