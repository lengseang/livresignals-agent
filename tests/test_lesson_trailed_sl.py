"""The stored `sl` is not always the DESIGNED stop — the trail overwrites it.

Found 2026-10-18 on `data/unified-paper.json` (3,041 closed with pnl).

`unified_trader.py:346-350` (the paper manage loop) MUTATES `p["sl"]` in place
once a trade is green:

    if pnl_pct >= 1.5 and p.get("sl", 0) < p["entry"] * 1.012:
        p["sl"] = round(p["entry"] * 1.012, 4)     # trail to +1.2%
    elif pnl_pct >= 0.5 and p.get("sl", 0) < p["entry"]:
        p["sl"] = round(p["entry"], 4)             # lock breakeven

There is ONE `sl` field. The trail writes the *current* stop over the *designed*
stop, and the closed record keeps only the last value. So on any trade that
ratcheted, the stored geometry no longer describes the trade that was entered.

Measured on the book:

    sl > entry (trailed)   675 records   95.3% win   net +70.27
    sl < entry (intact)  1,557 records   24.7% win   net -61.11
    whole book           3,047 records   47.2% win   net +18.48

The trailed cohort is +70 while the book is +18 — i.e. the entire book profit sits
in the records whose geometry was rewritten. `_rr()` divides by `abs(entry-sl)`,
and for a trailed record that denominator is the *ratchet*, not the risk:

    ADA  entry 0.2198  tp 0.224745  sl 0.2216 (trailed)
         stored RR = 2.75      designed RR = 2.0

All 675 trailed records read RR >= 2.29 **only because the trail moved the stop
through the entry**. Their designed RRs are the entry functions' own constants:
1.5 (`_paper_loose_open`, `_pullback_open`) or 1.8 (`_imported_strategy_open`).

Consequence, recomputing the headline split on designed RR:

    stored sl :  rr>=2  n=942  WR 74.4%  net +58.28   rr<2  n=1284  net -49.11
    designed  :  rr>=2  n=471  WR 53.9%  net  +7.60   rr<2  n=1755  net  +1.57

**471 trades that the loop labels "inside the winning population" were designed
at RR 1.5 or 1.8 — below the threshold it is judged against.** And the RR<2
population, which the rollup tells the owner "loses money, skipping them is the
single largest improvement available", is net POSITIVE. The recommendation was an
artefact of the trail.

This is the `best_px`-watermark pitfall in the skill, in a field the skill had
already written down as safe: `sl` is conditioned on the OUTCOME (the trail only
fires after the price went up), so measuring "risk" from it measures survivors.

STILL UNRESOLVED and deliberately not claimed here: whether the trail itself
helps. The trail's own record is what got destroyed, so this fix restores
JUDGEABILITY; it does not by itself re-derive the stop-width or RR findings.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path("/opt/trading-agent")
LOOP = Path("/home/ubuntu/hermes-home/scripts/trade_lesson_loop.py")

pytestmark = pytest.mark.skipif(
    not LOOP.exists(), reason="agent-home lesson loop not present in this checkout")

VALID_POP = {"instrument_invalid": False, "instrument_evidence": {}}


def _load_loop():
    spec = importlib.util.spec_from_file_location("trade_lesson_loop", LOOP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["trade_lesson_loop"] = mod
    spec.loader.exec_module(mod)
    return mod


def _loose_trade():
    """A REAL trailed loose record, copied verbatim from `data/unified-paper.json`.

    Loose lane is `stop = 0.012`, `tp = c*(1+stop*1.5)` -> designed RR 1.5.
    The trail then set sl = entry*1.012 (0.2198*1.012 = 0.22244, stored 0.2216
    after the earlier +0.5% breakeven step), which makes the STORED rr 2.747.
    """
    return {"symbol": "ADA/USDT", "tf": "30m", "entry": 0.2198,
            "tp": 0.224745, "sl": 0.2216, "size": 5.0, "loose": True,
            "ts_utc": "2026-09-07T10:45:46.844864+00:00", "pnl": 0.025, "reason": "SL"}


def test_a_trailed_sl_does_not_inflate_rr(  ):
    """The stored sl is the ratchet, not the risk. RR must come from the design."""
    loop = _load_loop()
    t = _loose_trade()
    stored = abs(t["tp"] - t["entry"]) / abs(t["entry"] - t["sl"])
    assert stored > 2.5, f"precondition: this record reads RR {stored:.2f} from the trail"
    assert abs(loop._rr(t) - 1.5) < 0.01, (
        f"a 1.5-designed trade was read as RR {loop._rr(t):.2f} because the trail "
        "moved sl above entry")


def test_a_trailed_loose_win_is_not_promoted_to_a_good_setup():
    """471 real trades are currently called 'good setup' on the trail's arithmetic."""
    loop = _load_loop()
    t = _loose_trade()
    verdict, note = loop.classify(t, VALID_POP)
    assert verdict == "GOOD OUTCOME, POOR DECISION", (
        f"a RR 1.5 design that won was classified {verdict}: {note}")


def test_a_trailed_imported_trade_reads_1_8():
    """The other real trailed design: `_imported_strategy_open`, stop 1.2%, k=1.8."""
    loop = _load_loop()
    t = {"symbol": "AVAX/USDT", "tf": "5m", "entry": 7.942, "tp": 8.113547,
         "sl": 8.0055, "size": 5.0, "edge": "supertrend", "imported": True,
         "ts_utc": "2026-09-07T09:16:15.748769+00:00", "pnl": 0.0397, "reason": "SL"}
    stored = abs(t["tp"] - t["entry"]) / abs(t["entry"] - t["sl"])
    assert stored > 2.5, f"precondition: reads RR {stored:.2f} from the trail"
    assert abs(loop._rr(t) - 1.8) < 0.01, loop._rr(t)


def test_an_untrailed_trade_still_reads_its_own_geometry():
    """The fix must not disturb the 1,557 records whose sl was never touched."""
    loop = _load_loop()
    t = {"symbol": "XRP/USDT", "ts_utc": "2026-10-18T00:00:00+00:00",
         "entry": 100.0, "tp": 103.0, "sl": 98.5, "pnl": -0.05, "reason": "SL"}
    assert abs(loop._rr(t) - 2.0) < 1e-9, loop._rr(t)


def test_the_designed_rr_is_exposed_so_the_conflict_is_visible():
    """A reader must be able to see BOTH numbers, not just the corrected one."""
    loop = _load_loop()
    t = _loose_trade()
    info = loop.geometry(t)
    assert abs(info["rr"] - 1.5) < 0.01
    assert info["sl_trailed"] is True
    assert info["rr_stored"] is not None and info["rr_stored"] > 2.5


def test_breakeven_lock_is_also_a_trail_not_a_zero_risk_stop():
    """`sl = entry` is the +0.5% lock, and it appears on 815 real records.

    `_rr()` divides by `abs(entry - sl)`, so those returned None and were written
    as UNKNOWN — 27% of the book marked unjudgeable by its own ratchet. The trade
    was judgeable all along: the DESIGNED stop is the entry function's constant.
    """
    loop = _load_loop()
    t = {"symbol": "JUP/USDT", "entry": 0.2559, "tp": 0.266831, "sl": 0.2559,
         "size": 5.0, "loose": True, "ts_utc": "2026-09-08T00:00:00+00:00",
         "pnl": 0.01, "reason": "SL"}
    assert loop._rr(t) is not None, "zero-risk denominator made a judgeable trade UNKNOWN"
    assert abs(loop._rr(t) - 1.5) < 0.02, loop._rr(t)


def test_unknown_geometry_is_still_unknown():
    loop = _load_loop()
    t = {"symbol": "X", "ts_utc": "2026-10-18T00:00:00+00:00",
         "entry": None, "tp": None, "sl": None, "pnl": 0.0, "reason": "TIME"}
    assert loop._rr(t) is None
    assert loop.classify(t, VALID_POP)[0] == "UNKNOWN"
