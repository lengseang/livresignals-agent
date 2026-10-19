"""`main()` must define every name its write loop uses, and must not corrupt
the ledger when it fails.

Measured 2026-10-19: commit 07869cc moved the RR<2 half-split out of a string
literal and into a DERIVED value, but only wired it into `repair_lessons()`
and `write_rollup()`. `main()`'s write loop still referenced a bare `split`,
which nothing in `main()` defined. The hourly cron therefore crashed with
`NameError: name 'split' is not defined` on EVERY run that had new trades —
24 hours, 6 ensemble + 20 unified trades never learned — while stdout happily
printed `new from unified : 20` first.

The whole existing suite was green through that, because it tested
`improve()` / `rr_split()` / `repair_lessons()` and never called `main()`.
These tests call `main()`.
"""
from __future__ import annotations

import json
import importlib.util
import sys
from pathlib import Path

import pytest

SRC = Path("/home/ubuntu/hermes-home/scripts/trade_lesson_loop.py")


@pytest.fixture()
def loop(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("tll_main_under_test", SRC)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)

    lessons = tmp_path / "trade-lessons.jsonl"
    rollup = tmp_path / "trade-lessons.json"
    monkeypatch.setattr(m, "LESSONS", lessons)
    monkeypatch.setattr(m, "ROLLUP", rollup)
    # main() memoises the split by (size, mtime) of the REAL ledger path;
    # the patch above moves the path, so reset the cache with it.
    monkeypatch.setattr(m, "_SPLIT_CACHE", {"key": None, "val": None})

    def trade(sym, ts, pnl, book="unified"):
        return {"symbol": sym, "ts_utc": ts, "entry": 100.0, "tp": 103.0,
                "sl": 98.5, "pnl": pnl, "reason": "TP" if pnl > 0 else "SL",
                "size": 5.0}

    def set_books(unified=(), ensemble=()):
        monkeypatch.setattr(m, "PAPER_BOOKS", [
            ("unified", "u.json"), ("ensemble", "e.json")])
        monkeypatch.setattr(
            m, "_load",
            lambda p, d=None: {"closed": list(unified if p == "u.json" else ensemble)})
    m.set_books = set_books
    m.make_trade = trade
    return m


def _rows(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def test_main_writes_every_new_trade(loop, capsys):
    """The exact regression: a run WITH new trades must append, not crash."""
    loop.set_books(unified=[loop.make_trade("ETH/USDT", "2026-10-19T10:00:00+00:00", 0.12)],
                   ensemble=[loop.make_trade("BTC/USDT", "2026-10-19T11:00:00+00:00", -0.05)])
    rc = loop.main()
    out = capsys.readouterr().out
    assert rc == 0, f"main() returned {rc}; stdout={out!r}"
    assert "not defined" not in out
    rows = _rows(loop.LESSONS)
    assert len(rows) == 2
    assert {r["book"] for r in rows} == {"unified", "ensemble"}
    for r in rows:
        assert r["what_would_improve"], "the derived-split note must be written"


def test_main_is_idempotent_on_a_second_run(loop):
    loop.set_books(unified=[loop.make_trade("ETH/USDT", "2026-10-19T10:00:00+00:00", 0.12)])
    assert loop.main() == 0
    before = loop.LESSONS.read_bytes()
    assert loop.main() == 0
    assert loop.LESSONS.read_bytes() == before, "a quiet run must not append"


def test_a_failure_mid_write_leaves_a_resumable_ledger(loop, monkeypatch):
    """If the write loop dies, the tail must not be a torn JSON line."""
    loop.set_books(unified=[loop.make_trade(f"ETH/USDT", f"2026-10-19T1{i}:00:00+00:00", 0.1)
                            for i in range(3)])
    real_improve = loop.improve
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash")
        return real_improve(*a, **k)

    monkeypatch.setattr(loop, "improve", boom)
    rc = loop.main()
    assert rc == 1
    # whatever is on disk must be complete, parseable lines
    rows = _rows(loop.LESSONS)
    assert len(rows) == 1
    assert loop.LESSONS.read_bytes().endswith(b"\n")


def test_drop_torn_tail_repairs_both_torn_shapes(loop, monkeypatch):
    good = json.dumps({"id": "a", "verdict": "ALIGNED"})
    for torn in (b'{"id": "b", "verd',                    # no trailing newline
                 b'{"id": "b", "verd\n',                  # newline, body torn
                 b'{"id": "b", "verd\\n'):                # escaped, incomplete
        loop.LESSONS.write_bytes(good.encode() + b"\n" + torn)
        with open(loop.LESSONS, "a", encoding="utf-8") as f:
            loop._drop_torn_tail(f)
        assert loop.LESSONS.read_bytes() == good.encode() + b"\n", torn


def test_drop_torn_tail_is_a_noop_on_a_healthy_ledger(loop):
    good = (json.dumps({"id": "a"}) + "\n" + json.dumps({"id": "b"}) + "\n").encode()
    loop.LESSONS.write_bytes(good)
    with open(loop.LESSONS, "a", encoding="utf-8") as f:
        loop._drop_torn_tail(f)
    assert loop.LESSONS.read_bytes() == good
