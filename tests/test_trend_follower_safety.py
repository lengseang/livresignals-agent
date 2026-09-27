"""F1: trend_follower.py must never buy the same coin twice after a crash (design: docs/loop/designs/T0.3.md).

The real exchange client is replaced by FakeExchange BEFORE main() imports it, so these tests cannot place
an order. Run: python -m pytest tests/test_trend_follower_safety.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DAY = 86_400_000


class Crash(BaseException):
    """Simulates the process dying (OOM / kill) — not an Exception, so the script's handlers can't swallow it."""


class OrderNotFound(Exception):
    pass


class FakeExchange:
    """Only what trend_follower.main() calls. Rising coins, BTC above its SMA200, all orders recorded."""

    def __init__(self, wallet=None, crash_after_buy=False, fill_average=True):
        self.orders: list[dict] = []
        self.by_cid: dict[str, dict] = {}
        self.wallet = {"USDT": {"free": 100.0, "total": 100.0}, **(wallet or {})}
        self.crash_after_buy = crash_after_buy
        self.fill_average = fill_average
        self.fetch_order_error: Exception | None = None

    def fetch_ohlcv(self, symbol, timeframe="1d", limit=300):
        up = [100.0 + i for i in range(300)]                   # every coin trends up, BTC above SMA200
        return [[i * DAY, c, c, c, c, 1.0] for i, c in enumerate(up)]

    def fetch_balance(self):
        return self.wallet

    def market(self, symbol):
        return {"precision": {"amount": 0.001}, "limits": {"amount": {"min": 0.0}}}

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        cid = (params or {}).get("clientOrderId")
        od = {"id": str(len(self.orders) + 1), "clientOrderId": cid, "symbol": symbol, "side": side,
              "amount": amount, "filled": amount, "status": "closed",
              "average": 399.0 if self.fill_average else None}
        self.orders.append(od)
        if cid:
            self.by_cid[cid] = od
        if side == "buy" and self.crash_after_buy:
            raise Crash("killed right after the exchange accepted the order")
        return od

    def fetch_order(self, id, symbol=None, params=None):
        if self.fetch_order_error:
            raise self.fetch_order_error
        cid = (params or {}).get("clientOrderId")
        if cid not in self.by_cid:
            raise OrderNotFound(cid)
        return self.by_cid[cid]


def load_tf(tmp_path, monkeypatch, ex: FakeExchange):
    monkeypatch.setenv("TREND_REAL", "1")
    fake_client = types.ModuleType("trading.exchange.live_client")
    fake_client.make_live_exchange = lambda: ex
    monkeypatch.setitem(sys.modules, "trading.exchange.live_client", fake_client)
    fake_ccxt = types.ModuleType("ccxt")
    fake_ccxt.OrderNotFound = OrderNotFound
    monkeypatch.setitem(sys.modules, "ccxt", fake_ccxt)
    spec = importlib.util.spec_from_file_location("tf_under_test", ROOT / "scripts" / "trend_follower.py")
    tf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tf)
    monkeypatch.setattr(tf, "STATE", tmp_path / "trend-state.json")
    monkeypatch.setattr(tf, "LOCK", tmp_path / "trend-follower.lock")
    monkeypatch.setattr(tf, "SYMS", ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"])
    tf.alerts = []
    monkeypatch.setattr(tf, "notify", lambda title, body: tf.alerts.append((title, body)))
    return tf


def buys(ex):
    return [o for o in ex.orders if o["side"] == "buy"]


def test_a_crash_after_the_order_does_not_cause_a_second_buy(tmp_path, monkeypatch):
    ex = FakeExchange(crash_after_buy=True)
    tf = load_tf(tmp_path, monkeypatch, ex)
    with pytest.raises(Crash):
        tf.main()
    first = buys(ex)
    assert len(first) == 1                                   # the order went through, then the process died
    ex.crash_after_buy = False
    tf.main()                                                 # next run must find that order, not buy again
    st = json.loads((tmp_path / "trend-state.json").read_text())
    held = [p["symbol"] for p in st["real"]["open"]]
    assert first[0]["symbol"] in held
    assert [o["symbol"] for o in buys(ex)].count(first[0]["symbol"]) == 1
    assert st["real"].get("pending", []) == []


def test_every_buy_is_saved_as_an_intent_before_it_is_sent(tmp_path, monkeypatch):
    ex = FakeExchange()
    tf = load_tf(tmp_path, monkeypatch, ex)
    seen = []
    real_create = ex.create_order

    def spy(*a, **k):
        st = json.loads((tmp_path / "trend-state.json").read_text())
        seen.append([p["cid"] for p in st["real"].get("pending", [])])
        return real_create(*a, **k)

    ex.create_order = spy
    tf.main()
    assert seen and all(k["clientOrderId"] in pend for k, pend in zip([o for o in ex.orders], seen))


def test_a_truncated_state_file_stops_trading_instead_of_starting_empty(tmp_path, monkeypatch):
    ex = FakeExchange()
    tf = load_tf(tmp_path, monkeypatch, ex)
    (tmp_path / "trend-state.json").write_text('{"positions": {}, "real": {"open": [{"sym', encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        tf.main()
    assert e.value.code != 0
    assert ex.orders == []
    assert len(tf.alerts) == 1


def test_an_unreachable_order_status_blocks_new_buys_and_keeps_the_intent(tmp_path, monkeypatch):
    ex = FakeExchange()
    tf = load_tf(tmp_path, monkeypatch, ex)
    now_min = tf.datetime.now(tf.timezone.utc).strftime("%Y%m%d%H%M")
    state = {"positions": {}, "log": [], "real": {"open": [], "pending": [
        {"cid": f"tf-ETH-{now_min}-abc123", "symbol": "ETH/USDT", "qty": 0.01,
         "ts": tf.datetime.now(tf.timezone.utc).isoformat(), "state": "sent"}]}}
    (tmp_path / "trend-state.json").write_text(json.dumps(state), encoding="utf-8")
    ex.fetch_order_error = ConnectionError("network down")
    tf.main()
    st = json.loads((tmp_path / "trend-state.json").read_text())
    assert buys(ex) == []                                     # unsure whether we already bought -> buy nothing
    assert len(st["real"]["pending"]) == 1


def test_an_intent_that_never_reached_the_exchange_is_dropped(tmp_path, monkeypatch):
    ex = FakeExchange()
    tf = load_tf(tmp_path, monkeypatch, ex)
    state = {"positions": {}, "log": [], "real": {"open": [], "pending": [
        {"cid": "tf-ETH-202609270000-dead00", "symbol": "ETH/USDT", "qty": 0.01,
         "ts": tf.datetime.now(tf.timezone.utc).isoformat(), "state": "sent"}]}}
    (tmp_path / "trend-state.json").write_text(json.dumps(state), encoding="utf-8")
    tf.main()
    st = json.loads((tmp_path / "trend-state.json").read_text())
    assert st["real"]["pending"] == []
    assert len(buys(ex)) >= 1                                  # normal trading resumes once it's resolved


def test_a_fill_without_an_average_price_is_recorded_as_unknown(tmp_path, monkeypatch):
    ex = FakeExchange(fill_average=False)
    tf = load_tf(tmp_path, monkeypatch, ex)
    tf.main()
    st = json.loads((tmp_path / "trend-state.json").read_text())
    assert st["real"]["open"] and all(p["entry"] is None and p["entry_status"] == "UNKNOWN" for p in st["real"]["open"])


def test_the_wallet_cap_blocks_buys_when_the_wallet_already_holds_max_pos_coins(tmp_path, monkeypatch):
    # state knows nothing, but the wallet already holds 3 real coins worth > $1 each (e.g. after a lost state file)
    wallet = {c: {"free": 1.0, "total": 1.0} for c in ("BTC", "ETH", "SOL")}
    ex = FakeExchange(wallet=wallet)
    tf = load_tf(tmp_path, monkeypatch, ex)
    tf.main()
    assert buys(ex) == []


def test_dust_below_one_dollar_does_not_count_as_held(tmp_path, monkeypatch):
    wallet = {c: {"free": 0.001, "total": 0.001} for c in ("BTC", "ETH", "SOL")}   # 0.001 * $399 < $1
    ex = FakeExchange(wallet=wallet)
    tf = load_tf(tmp_path, monkeypatch, ex)
    tf.main()
    assert len(buys(ex)) >= 1


def test_a_second_run_while_the_first_holds_the_lock_does_nothing(tmp_path, monkeypatch):
    ex = FakeExchange()
    tf = load_tf(tmp_path, monkeypatch, ex)
    held = tf._acquire_lock(tf.LOCK)
    try:
        with pytest.raises(SystemExit):
            tf.main()
        assert ex.orders == []
    finally:
        tf._release_lock(held)


def test_state_is_written_atomically(tmp_path, monkeypatch):
    ex = FakeExchange()
    tf = load_tf(tmp_path, monkeypatch, ex)
    tf.save({"positions": {}, "log": [], "real": {"open": []}})
    monkeypatch.setattr(tf.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        tf.save({"positions": {"X": 1}, "log": [], "real": {"open": []}})
    assert json.loads((tmp_path / "trend-state.json").read_text())["positions"] == {}   # old version intact
