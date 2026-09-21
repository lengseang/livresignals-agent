"""P-A PnL tracker + replay tests."""
from __future__ import annotations

import json

import pytest

from trading.allocation import pnl, replay_pnl
from trading.allocation.testnet_executor import save_ledger


def test_compute_pnl_shape(tmp_path, monkeypatch):
    # Use a ledger without hitting network: monkeypatch fetch_prices
    monkeypatch.setattr(pnl, "fetch_prices", lambda: {"BTC/USDT": 63000.0, "ETH/USDT": 1850.0, "SOL/USDT": 75.0})
    # Patch BOTH modules' ledger path so nothing writes to the real file.
    from trading.allocation import testnet_executor as te
    monkeypatch.setattr(pnl, "LEDGER_FILE", tmp_path / "ledger.json")
    monkeypatch.setattr(te, "LEDGER_FILE", tmp_path / "ledger.json")
    save_ledger({"entries": [
        {"symbol": "BTC/USDT", "qty": 0.001, "amount_usdt": 60.0},
        {"symbol": "ETH/USDT", "qty": 0.05, "amount_usdt": 92.5},
    ]})
    monkeypatch.setattr(pnl, "REPORTS_DIR", tmp_path)
    r = pnl.compute_pnl()
    # 0.001 * 63000 = 63.0 vs cost 60.0 -> +3.0
    assert r["symbols"]["BTC/USDT"]["pnl"] == pytest.approx(3.0, abs=1e-6)
    assert "portfolio" in r


def test_compute_pnl_no_positions(tmp_path, monkeypatch):
    monkeypatch.setattr(pnl, "fetch_prices", lambda: {"BTC/USDT": 63000.0, "ETH/USDT": 1850.0, "SOL/USDT": 75.0})
    from trading.allocation import testnet_executor as te
    monkeypatch.setattr(pnl, "LEDGER_FILE", tmp_path / "ledger2.json")
    monkeypatch.setattr(te, "LEDGER_FILE", tmp_path / "ledger2.json")
    save_ledger({"entries": []})
    monkeypatch.setattr(pnl, "REPORTS_DIR", tmp_path)
    r = pnl.compute_pnl()
    assert r["portfolio"]["cost_basis"] == 0.0
    assert r["portfolio"]["unrealized_pnl"] == 0.0


def test_append_history_append_only(tmp_path, monkeypatch):
    monkeypatch.setattr(pnl, "PNL_HISTORY", tmp_path / "history.csv")
    pnl.append_history({
        "generated_at_utc": "2026-08-12T00:00:00Z",
        "portfolio": {"cost_basis": 100.0, "market_value": 110.0, "unrealized_pnl": 10.0, "unrealized_pnl_pct": 10.0},
    })
    pnl.append_history({
        "generated_at_utc": "2026-08-12T01:00:00Z",
        "portfolio": {"cost_basis": 100.0, "market_value": 120.0, "unrealized_pnl": 20.0, "unrealized_pnl_pct": 20.0},
    })
    lines = (tmp_path / "history.csv").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3  # header + 2 rows


def test_replay_returns_report(tmp_path, monkeypatch):
    # Inject tiny fake closes to exercise replay without the DB
    import sqlite3
    db = tmp_path / "r.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE candles (symbol TEXT, timeframe TEXT, open_time_utc INTEGER, close REAL)")
    n = 400
    for s in ("BTC/USDT", "ETH/USDT", "SOL/USDT"):
        base = 100.0
        for i in range(n):
            base *= 1.001  # gentle uptrend
            con.execute("INSERT INTO candles VALUES (?,?,?,?)", (s, "1d", i * 86400_000, base))
    con.commit()
    con.close()
    monkeypatch.setattr(replay_pnl, "SQLITE_PATH", db)
    monkeypatch.setattr(replay_pnl, "REPORTS_DIR", tmp_path)
    from trading.allocation.engine import AllocationConfig
    r = replay_pnl.replay(["BTC/USDT", "ETH/USDT", "SOL/USDT"], AllocationConfig(interval_days=30, lookback_days=90))
    assert r["final_cost"] > 0
    assert r["total_pnl_pct"] != 0  # gentle uptrend -> positive
    assert "positions" in r


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))