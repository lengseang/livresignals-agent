"""F2: a failed real sell must not strand the position (design: docs/loop/designs/T0.4.md).

Same harness as test_trend_follower_safety.py: FakeExchange + fake ccxt, so nothing reaches the network.
The fake ccxt mirrors the real hierarchy that matters here: OrderNotFound IS an InvalidOrder.
"""
from __future__ import annotations

import json
import sys
import types

from test_trend_follower_safety import DAY, FakeExchange, load_tf


class InvalidOrder(Exception):
    pass


class OrderNotFound(InvalidOrder):
    pass


class NetworkError(Exception):
    pass


RISING = [100.0 + i for i in range(300)]          # last 399, above SMA50
FALLING = [400.0 - i for i in range(300)]         # last 101, below SMA50: trend broken


class ExitExchange(FakeExchange):
    def __init__(self, curves=None, sell_errors=(), **kw):
        super().__init__(**kw)
        self.curves = curves or {}
        self.sell_errors = list(sell_errors)       # raised by successive sells, BEFORE Binance sees them
        self.sells_sent = 0

    def fetch_ohlcv(self, symbol, timeframe="1d", limit=300):
        c = self.curves.get(symbol, RISING)
        return [[i * DAY, x, x, x, x, 1.0] for i, x in enumerate(c)]

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        if side == "sell":
            self.sells_sent += 1
            if self.sell_errors:
                raise self.sell_errors.pop(0)
        return super().create_order(symbol, type_, side, amount, price, params)

    def fetch_order(self, id, symbol=None, params=None):
        if self.fetch_order_error:
            raise self.fetch_order_error
        cid = (params or {}).get("clientOrderId")
        if cid not in self.by_cid:
            raise OrderNotFound(cid)
        return self.by_cid[cid]


def setup(tmp_path, monkeypatch, ex, open_=None, positions=None):
    tf = load_tf(tmp_path, monkeypatch, ex)
    fake = types.ModuleType("ccxt")
    fake.OrderNotFound, fake.InvalidOrder, fake.NetworkError = OrderNotFound, InvalidOrder, NetworkError
    monkeypatch.setitem(sys.modules, "ccxt", fake)
    rp = {"symbol": "ETH/USDT", "qty": 1.0, "entry": 399.0, "entry_status": "FILL", "cid": "tf-ETH-x",
          "ts": "2026-09-01T00:00:00+00:00"}
    st = {"positions": positions or {}, "log": [],
          "real": {"open": open_ if open_ is not None else [rp], "pending": []}}
    (tmp_path / "trend-state.json").write_text(json.dumps(st), encoding="utf-8")
    ex.wallet["ETH"] = {"free": 1.0, "total": 1.0}
    return tf


def state(tmp_path):
    return json.loads((tmp_path / "trend-state.json").read_text())


def sells(ex):
    return [o for o in ex.orders if o["side"] == "sell"]


def test_a_network_error_keeps_the_position_and_the_next_run_retries(tmp_path, monkeypatch):
    ex = ExitExchange(curves={"ETH/USDT": FALLING}, sell_errors=[NetworkError("timeout")])
    tf = setup(tmp_path, monkeypatch, ex)
    tf.main()
    st = state(tmp_path)
    eth = [p for p in st["real"]["open"] if p["symbol"] == "ETH/USDT"]
    assert len(eth) == 1
    assert eth[0]["exit_pending"]["attempts"] == 1
    tf.main()
    st = state(tmp_path)
    assert "ETH/USDT" not in [p["symbol"] for p in st["real"]["open"]] and len(sells(ex)) == 1
    assert st["real"]["closed"][0]["attempts"] == 2


def test_the_hard_stop_applies_even_when_paper_is_flat(tmp_path, monkeypatch):
    ex = ExitExchange()                                         # trend intact, px 399
    rp = {"symbol": "ETH/USDT", "qty": 1.0, "entry": 500.0, "entry_status": "FILL", "ts": "t"}   # -20%
    tf = setup(tmp_path, monkeypatch, ex, open_=[rp], positions={"ETH/USDT": {"long": False, "entry": 0}})
    tf.main()
    closed = state(tmp_path)["real"]["closed"]
    assert closed[0]["reason"] == "HARD-STOP" and closed[0]["stop_basis"] == "real"


def test_a_coin_already_held_for_real_is_not_bought_again(tmp_path, monkeypatch):
    ex = ExitExchange(curves={"ETH/USDT": [100.0 + 2 * i for i in range(300)]})   # ETH ranks first by momentum
    tf = setup(tmp_path, monkeypatch, ex)
    tf.main()
    assert "ETH/USDT" not in [o["symbol"] for o in ex.orders if o["side"] == "buy"]


def test_it_sells_only_what_the_wallet_holds(tmp_path, monkeypatch):
    ex = ExitExchange(curves={"ETH/USDT": FALLING})
    tf = setup(tmp_path, monkeypatch, ex)
    ex.wallet["ETH"] = {"free": 0.5, "total": 0.5}             # booked 1.0
    tf.main()
    assert [o["amount"] for o in sells(ex)] == [0.5]


def test_an_invalid_order_blocks_the_exit_and_alerts_once(tmp_path, monkeypatch):
    ex = ExitExchange(curves={"ETH/USDT": FALLING}, sell_errors=[InvalidOrder("-2010 below min notional")])
    tf = setup(tmp_path, monkeypatch, ex)
    tf.main()
    tf.main()
    st = state(tmp_path)
    assert st["real"]["open"][0]["exit_blocked"]
    assert ex.sells_sent == 1
    assert len([a for a in tf.alerts if a[0].startswith("⛔ Real exit blocked")]) == 1


def test_no_sellable_balance_keeps_the_record_instead_of_deleting_it(tmp_path, monkeypatch):
    ex = ExitExchange(curves={"ETH/USDT": FALLING})
    tf = setup(tmp_path, monkeypatch, ex)
    ex.wallet["ETH"] = {"free": 0.0, "total": 0.0}
    tf.main()
    st = state(tmp_path)
    assert st["real"]["open"][0]["exit_blocked"] and sells(ex) == []


def test_a_sell_without_an_average_price_is_recorded_as_unknown(tmp_path, monkeypatch):
    ex = ExitExchange(curves={"ETH/USDT": FALLING}, fill_average=False)
    tf = setup(tmp_path, monkeypatch, ex)
    tf.main()
    c = state(tmp_path)["real"]["closed"][0]
    assert c["exit"] is None and c["exit_status"] == "UNKNOWN"


def test_a_previous_sell_that_filled_is_booked_not_sent_again(tmp_path, monkeypatch):
    ex = ExitExchange(curves={"ETH/USDT": FALLING})
    ex.by_cid["tf-ETH-sold"] = {"id": "9", "status": "closed", "filled": 1.0, "average": 150.0}
    rp = {"symbol": "ETH/USDT", "qty": 1.0, "entry": 399.0, "ts": "t",
          "exit_pending": {"since": "t", "attempts": 1, "why": "trend-broke", "cid": "tf-ETH-sold"}}
    tf = setup(tmp_path, monkeypatch, ex, open_=[rp])
    tf.main()
    c = state(tmp_path)["real"]["closed"][0]
    assert ex.sells_sent == 0
    assert c["exit"] == 150.0 and c["entry_status"] == "BOOKED"   # a pre-F1 entry is not called a fill


def test_an_unreachable_sell_status_sends_nothing(tmp_path, monkeypatch):
    ex = ExitExchange(curves={"ETH/USDT": FALLING})
    ex.fetch_order_error = NetworkError("down")
    rp = {"symbol": "ETH/USDT", "qty": 1.0, "entry": 399.0, "ts": "t",
          "exit_pending": {"since": "t", "attempts": 1, "why": "trend-broke", "cid": "tf-ETH-lost"}}
    tf = setup(tmp_path, monkeypatch, ex, open_=[rp])
    tf.main()
    assert ex.sells_sent == 0 and state(tmp_path)["real"]["open"]


def test_real_exits_never_write_to_the_paper_log(tmp_path, monkeypatch):
    ex = ExitExchange(curves={"ETH/USDT": FALLING})
    tf = setup(tmp_path, monkeypatch, ex)                         # paper doesn't hold ETH
    tf.main()
    st = state(tmp_path)
    assert st["real"]["closed"] and all(e.get("book") != "real" for e in st["log"])
    assert not any(e["symbol"] == "ETH/USDT" for e in st["log"])


def test_a_paper_exit_alone_does_not_sell_real_money(tmp_path, monkeypatch):
    ex = ExitExchange()                                         # trend intact
    tf = setup(tmp_path, monkeypatch, ex, positions={"ETH/USDT": {"long": True, "entry": 600.0}})  # paper -33%
    tf.main()
    st = state(tmp_path)
    assert sells(ex) == [] and st["real"]["open"]              # real entry 399: no stop, no break
