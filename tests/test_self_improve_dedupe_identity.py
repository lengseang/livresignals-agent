"""`dedupe` must remove only TRUE re-bookings, never two distinct trades.

Context (2026-10-14, overnight shift): `scripts/self_improve.py` deduped the
books with the key

    ("symbol", "entry", "exit", "close_ts_utc")   # each str()-truncated to 16

On the REAL book that works — real records carry `exit` + `close_ts_utc`. On the
PAPER book it is catastrophic: paper records leave both None (measured: only 2
of 2941 set them), so the key degenerates to `(symbol, entry)` and EVERY trade
ever opened at the same price on a symbol — months apart, different TP/SL,
different outcomes — collapses into one.

Measured consequences:
  * `paper_c` came out at n=2229 while the book holds 2941 distinct closes —
    712 real trades (24%) dropped, hiding +7.14 gross / ~+2.7 net.
  * it FLIPPED DISABLE DECISIONS (which write the real-money open path):
      edge 'momentum'  truncated n=24 wr=29% net=-0.18  -> DISABLE
                       full      n=30 wr=40% net=+0.19  -> keep
      edge 'rsi2'      truncated n=5  (below the n>=15 floor, never judged)
                       full      n=27 wr=56%
      edge 'loose'     truncated net=-0.264 (negative) vs full +0.144 (positive)

The lesson loop already used the correct identity (symbol|ts_utc|entry|size|pnl).
`dedupe` now defaults to the same shape: symbol + OPEN time + entry + size.

Run:  ./venv/bin/python -m pytest tests/test_self_improve_dedupe_identity.py -q
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _paper(symbol, entry, ts, pnl, tp, sl):
    """A PAPER-schema closed record: no `exit`, no `close_ts_utc`."""
    return {"symbol": symbol, "entry": entry, "tp": tp, "sl": sl,
            "size": 5.0, "loose": False, "ts_utc": ts, "pnl": pnl,
            "reason": "TIME", "edge": "momentum"}


def test_same_price_same_symbol_distinct_times_are_kept():
    """The exact defect: two trades at one entry price must BOTH survive."""
    si = _load("self_improve")
    a = _paper("OP/USDT", 0.1223, "2026-09-19T06:16:40.072625+00:00", -0.0123, 0.126, 0.1223)
    b = _paper("OP/USDT", 0.1223, "2026-10-09T15:16:21.339923+00:00", +0.2330, 0.1272, 0.1199)
    out = si.dedupe([a, b])
    assert len(out) == 2, "same symbol+entry but different open time = distinct trades"
    # sanity: opposite outcomes were preserved, not collapsed to the first
    assert {t["pnl"] for t in out} == {-0.0123, 0.2330}


def test_true_rebooking_is_still_removed():
    """The dedupe must still do its job on a genuine double-write."""
    si = _load("self_improve")
    a = _paper("BTC/USDT", 79924.31, "2026-09-04T12:32:24.636067+00:00",
               -0.0063, 81722.6, 78725.4)
    dup = dict(a)  # byte-identical re-booking
    assert len(si.dedupe([a, dup])) == 1


def test_old_key_would_have_collapsed_them():
    """Pin the regression: the OLD key merges the two distinct trades."""
    si = _load("self_improve")
    a = _paper("OP/USDT", 0.1223, "2026-09-19T06:16:40.072625+00:00", -0.0123, 0.126, 0.1223)
    b = _paper("OP/USDT", 0.1223, "2026-10-09T15:16:21.339923+00:00", +0.2330, 0.1272, 0.1199)
    old = si.dedupe([a, b], ("symbol", "entry", "exit", "close_ts_utc"))
    assert len(old) == 1, "the old key collapses distinct paper trades — the defect"


def test_missing_size_pair_does_not_collide_live_records():
    """Live records use size_usdt; the identity must still separate them."""
    si = _load("self_improve")
    a = {"symbol": "XRP/USDT", "entry": 1.4, "size_usdt": 5.0,
         "open_ts_utc": "2026-10-11T21:26:07+00:00", "pnl": -0.04, "reason": "TIME"}
    b = {"symbol": "XRP/USDT", "entry": 1.4, "size_usdt": 5.0,
         "open_ts_utc": "2026-10-12T03:56:08+00:00", "pnl": 0.01, "reason": "TIME"}
    assert len(si.dedupe([a, b])) == 2
