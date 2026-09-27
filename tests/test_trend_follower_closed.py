"""F3: every real exit is recorded honestly in real.closed (design: docs/loop/designs/T0.5.md).

Harness from test_trend_follower_exits.py (fake exchange, fake ccxt, no network).
"""
from __future__ import annotations

from test_trend_follower_exits import FALLING, ExitExchange, sells, setup, state

OPEN_TS = "2026-09-01T00:00:00+00:00"
OPEN_MS = 1788220800000


class HistoryExchange(ExitExchange):
    def __init__(self, trades=None, history_error=None, fee=None, **kw):
        super().__init__(curves={"ETH/USDT": FALLING}, **kw)
        self.trades = trades or []
        self.history_error = history_error
        self.fee = fee

    def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):
        if self.history_error:
            raise self.history_error
        return [t for t in self.trades if t["timestamp"] >= (since or 0)]

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        od = super().create_order(symbol, type_, side, amount, price, params)
        if side == "sell" and self.fee:
            od["fee"] = self.fee
        return od


def closed(tmp_path):
    return state(tmp_path)["real"]["closed"]


def test_a_filled_sell_with_a_usdt_fee_has_fill_values_and_net_pnl(tmp_path, monkeypatch):
    ex = HistoryExchange(fee={"cost": 0.05, "currency": "USDT"})
    tf = setup(tmp_path, monkeypatch, ex)                      # entry 399, qty 1; fill average 399.0
    ex.fill_average = True
    tf.main()
    c = closed(tmp_path)[0]
    assert (c["exit_status"], c["qty_status"], c["fee_status"]) == ("FILL", "FILL", "FILL")
    assert c["pnl_gross_usd"] == 0.0 and c["pnl_usd"] == -0.05


def test_a_bnb_fee_is_kept_raw_and_net_pnl_stays_unknown(tmp_path, monkeypatch):
    ex = HistoryExchange(fee={"cost": 0.0001, "currency": "BNB"})
    tf = setup(tmp_path, monkeypatch, ex)
    tf.main()
    c = closed(tmp_path)[0]
    assert c["fee_usd"] is None and c["fee_status"] == "UNKNOWN"
    assert c["fee_raw"] == [{"cost": 0.0001, "currency": "BNB"}]
    assert c["pnl_gross_usd"] == 0.0 and c["pnl_usd"] is None


def test_a_sell_without_an_average_has_no_pnl(tmp_path, monkeypatch):
    ex = HistoryExchange(fill_average=False, fee={"cost": 0.05, "currency": "USDT"})
    tf = setup(tmp_path, monkeypatch, ex)
    tf.main()
    c = closed(tmp_path)[0]
    assert c["exit"] is None and c["pnl_gross_usd"] is None and c["pnl_usd"] is None


def test_coins_already_sold_are_closed_from_trade_history(tmp_path, monkeypatch):
    trades = [{"id": 7, "side": "sell", "amount": 0.6, "price": 300.0, "timestamp": OPEN_MS + 1,
               "fee": {"cost": 0.1, "currency": "USDT"}},
              {"id": 8, "side": "sell", "amount": 0.4, "price": 350.0, "timestamp": OPEN_MS + 2,
               "fee": {"cost": 0.1, "currency": "USDT"}},
              {"id": 6, "side": "sell", "amount": 5.0, "price": 1.0, "timestamp": OPEN_MS - 1}]   # before open
    ex = HistoryExchange(trades=trades)
    tf = setup(tmp_path, monkeypatch, ex)
    ex.wallet["ETH"] = {"free": 0.0, "total": 0.0}
    tf.main()
    c = closed(tmp_path)[0]
    assert sells(ex) == []
    assert c["reason"] == "NO_SELLABLE_BALANCE" and c["exit_status"] == "TRADE_HISTORY"
    assert c["exit"] == 320.0 and c["qty"] == 1.0 and c["trade_ids"] == ["7", "8"]
    assert "ETH/USDT" not in [p["symbol"] for p in state(tmp_path)["real"]["open"]]


def test_history_that_does_not_reconcile_is_unknown_not_guessed(tmp_path, monkeypatch):
    trades = [{"id": 7, "side": "sell", "amount": 0.3, "price": 300.0, "timestamp": OPEN_MS + 1}]
    ex = HistoryExchange(trades=trades)
    tf = setup(tmp_path, monkeypatch, ex)
    ex.wallet["ETH"] = {"free": 0.0, "total": 0.0}
    tf.main()
    c = closed(tmp_path)[0]
    assert c["exit"] is None and c["exit_status"] == "UNKNOWN" and "not reconciled" in c["exit_resolve_error"]
    assert any(a[0].startswith("⚠️ Real exit UNKNOWN") for a in tf.alerts)


def test_a_failed_history_lookup_still_records_the_close_as_unknown(tmp_path, monkeypatch):
    ex = HistoryExchange(history_error=RuntimeError("rate limited"))
    tf = setup(tmp_path, monkeypatch, ex)
    ex.wallet["ETH"] = {"free": 0.0, "total": 0.0}
    tf.main()
    c = closed(tmp_path)[0]
    assert c["exit_status"] == "UNKNOWN" and "rate limited" in c["exit_resolve_error"]
