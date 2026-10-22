"""The ensemble paper book held targets its own bar size could never reach.

Found 2026-10-21. `ensemble_paper.py` recorded `hold_bars` as the count of the
ENSEMBLE's timeframe bars, but that `hold` comes from the member id
(`family|symbol|tf|lb|hold|rr`) and belongs to the MEMBER's timeframe. For
`ens|momentum|5m` the target was sized off a 5m member (rr 1.5 -> +3%) and then
held for 48 bars of 5m — a horizon on which BTC closed above +3% only 1.9% of
the time. Result: 0 TP exits in 170 closes, 142 TIME, and a "-4.97 net" that
reads as a verdict on the momentum rule when it is a verdict on the geometry.

Four defects, all pinned here:
  1. no reachability check at entry (a target is now refused when
     P(reach it within the hold) is below MIN_P_MARGIN);
  2. `hold_bars` had no bar size beside it, so the manage loop could not age a
     position from the timestamp alone (verified: a 30m open_ms of 1792587600000
     is 13:00 UTC, NOT a multiple of 30m). `tf_ms` is persisted at entry;
  3. the ceiling was swept only to +3%, so a 6% target could never be
     certified — it came back "unmeasurable" for arithmetic reasons;
  4. the ceiling was measured over max(hold, lb) bars while the position is
     aged over `hold`, certifying a horizon the book never holds (real BTC 5m:
     2.1% at 96 bars vs 1.6% at 48).

Each test states what it pins; the negative controls (which must NOT refuse or
flag) are here because a gate that blocks everything is indistinguishable from a
broken gate.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path("/opt/trading-agent")
spec = importlib.util.spec_from_file_location(
    "ensemble_paper", ROOT / "scripts/ensemble_paper.py")
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)


def _bars(closes, tf_ms=300_000, start=0):
    # mid == close so the high (index 2) equals the close: margin_pct measures
    # upside from the close, wick-free.
    return [(start + i * tf_ms, c, c, c, c) for i, c in enumerate(closes)]


# ── _reach_freqs: the sweep itself ──────────────────────────────────────────

def test_reach_freqs_is_monotone_in_the_offset():
    # A series that wanders: P(reach +x) must never increase with x. Computed
    # frequencies do fall, but the guarantee must be structural — a ceiling that
    # relied on the data cooperating could certify a far target and refuse a near one.
    import random
    rng = random.Random(7)
    px, closes = 100.0, []
    for _ in range(3000):
        px *= 1 + rng.gauss(0, 0.004)
        closes.append(px)
    f = ep._reach_freqs(closes, 24, 6.0)
    levels = sorted(f)
    assert levels, "a 3000-bar walk must reach some level"
    assert all(f[levels[i]] >= f[levels[i + 1]] for i in range(len(levels) - 1))


def test_reach_freqs_sweeps_past_the_old_three_percent_cap():
    # +2% every bar: a 6% target is reached inside one bar, so the sweep must be
    # able to certify it. The old code swept only to 3.0 and returned 3.0 for
    # every target, which made a 6% target permanently "unmeasurable".
    rows = [c for c in (100.0 * (1.02 ** i) for i in range(60))]
    assert ep.margin_pct(_bars(rows), 48, target_pct=6.0) >= 6.0


def test_reach_freqs_withholds_the_zero_level():
    # P(>=0%) is true of every bar and is not a reachability statement.
    assert 0.0 not in ep._reach_freqs([100.0] * 50, 10, 3.0)


# ── margin_pct / is_unreachable ────────────────────────────────────────────

def test_margin_reachable_target():
    # +2% every bar: every entry reaches every level within a 48-bar horizon.
    rows = _bars([100.0 * (1.02 ** i) for i in range(200)])
    assert ep.margin_pct(rows, 48) == 3.5     # swept just past its default 3.0
    assert ep.is_unreachable(rows, 48, 3.0) is False


def test_margin_flat_market_reaches_nothing():
    # A perfectly flat series can never reach even +0.5% -> no claimable level.
    rows = _bars([100.0] * 200)
    assert ep.margin_pct(rows, 48) is None
    assert ep.is_unreachable(rows, 48, 3.0) is True


def _sawtooth(peak_pct=2.0, steps=50, cycles=60):
    """Bars that rise linearly from 100 to 100+peak_pct% then fall back.

    The fall mirrors the rise, so the price always retraces to 100: the only
    upside ever available from any bar is peak_pct. One rise takes `steps` bars.
    """
    up = [100.0 * (1 + peak_pct / 100 * k / steps) for k in range(steps + 1)]
    down = list(reversed(up[:-1]))
    return (up + down) * cycles


def test_margin_unreachable_is_below_target():
    # A +2%-tall sawtooth. The horizon must SPAN the rise (>= steps bars) for
    # near levels to be reachable at all; at 10 bars the price can only move
    # 0.4% and "nothing measurable" is the correct answer, not a ceiling.
    rows = _bars(_sawtooth(2.0))
    assert ep.margin_pct(rows, 10) is None            # horizon shorter than the rise
    m = ep.margin_pct(rows, 50)
    assert m is not None and 1.0 <= m < 3.0
    assert ep.is_unreachable(rows, 50, 3.0) is True
    assert ep.is_unreachable(rows, 50, 1.0) is False


def test_margin_stays_below_a_far_target():
    # Same sawtooth, horizon long enough that the whole cycle fits: still only
    # +2% is ever available, so a +3% target must not be certified reachable.
    rows = _bars(_sawtooth(2.0))
    m = ep.margin_pct(rows, 120)
    assert m is not None and m < 3.0
    assert ep.is_unreachable(rows, 120, 3.0) is True


def test_margin_is_walk_forward():
    # A jump in the LAST bars must not raise the ceiling for EARLIER bars.
    closes = [100.0] * 300 + [150.0, 200.0, 250.0, 300.0]
    rows = _bars(closes)
    m = ep.margin_pct(rows, 48)
    # Only 4 bars at the end can "reach" anything, and they are the last bars,
    # so no forward window starts before them and none sees the jump.
    assert m is None


# ── the hold horizon, not the lookback ─────────────────────────────────────

def test_ceiling_uses_the_hold_not_the_longer_lookback():
    # The horizon a position is AGED on is `hold` (manage: tf_ms * hold_bars).
    # Measuring over max(hold, lb) certifies a horizon the book never holds.
    # A drifting-then-quiet series: +2% arrives over many bars, so a short hold
    # and a long lookback give DIFFERENT ceilings and the choice is observable.
    closes, px = [], 100.0
    # 150 bars drifting up 0.12%/bar (cumulative ~+20%), then 300 bars flat:
    # measured ceilings 2.5% over a 24-bar hold vs 3.5% over 96 bars.
    for _ in range(150):
        px *= 1.0012
        closes.append(px)
    closes += [px] * 300
    rows = _bars(closes)
    short, long_ = ep.margin_pct(rows, 24), ep.margin_pct(rows, 96)
    assert short < long_, "the long horizon must certify more than the short one"
    # A target between the two ceilings is refused on the hold and admitted on
    # the lookback: the gate must follow the hold.
    mid = 3.0
    assert short < mid <= long_
    assert ep.is_unreachable(rows, 24, mid) is True
    assert ep.is_unreachable(rows, 96, mid) is False


# ── _hold_ms: a position is aged by ITS OWN bar size ───────────────────────

def test_hold_ms_uses_recorded_bar_size():
    assert ep._hold_ms({"tf_ms": 1_800_000, "hold_bars": 12}) == 21_600_000
    assert ep._hold_ms({"tf_ms": 300_000, "hold_bars": 48}) == 14_400_000


def test_hold_ms_refuses_to_guess():
    # A record with neither tf_ms nor tf_ms derivable is un-ageable: 0, never
    # a default bar size.
    assert ep._hold_ms({"hold_bars": 12}) == 0
    assert ep._hold_ms({"tf_ms": 0, "hold_bars": 12}) == 0
    assert ep._hold_ms({"tf_ms": 300_000}) == 0


def test_open_ms_is_not_clock_snapped():
    # The book's open_ms is the last CLOSED bar's open time, and the 5m series
    # is clock-snapped (12:05 is a multiple of 5m) while a 30m position on the
    # same symbol can be stamped 13:00 — a value that IS a multiple of 30m. So
    # the ms value carries no safe signal about which bar size aged it: the bar
    # size must be recorded, never inferred. This pins the real 5m record.
    assert 1792584300000 % 300_000 == 0        # 12:05, consistent with 5m
    assert ep._hold_ms({"hold_bars": 48}) == 0  # ...yet still refused without tf_ms


# ── the bugs together, through main(), on a copy of the book ───────────────

def _run_main(tmp_path, monkeypatch, rows, ens, book_state=None):
    import json
    book = tmp_path / "book.json"
    book.write_text(json.dumps(book_state or {"open": [], "closed": [], "meta": {}}))
    (tmp_path / "ensembles.json").write_text(json.dumps(ens))

    monkeypatch.setattr(ep, "BOOK", book)
    monkeypatch.setattr(ep, "ENSEMBLES", tmp_path / "ensembles.json")
    monkeypatch.setattr(ep, "_candles", lambda cur, sym, tf, limit: rows[-max(limit, 5):])
    monkeypatch.setattr(ep, "_rule_fires", lambda *a, **k: True)
    # No long history in this fake DB: the year-window check must report
    # "untested" and must NOT rescue or block anything.
    monkeypatch.setattr(ep, "long_window_verdict",
                        lambda *a, **k: {"tested": False, "reason": "fixture"})

    class FakeCur:
        connection = None

    class FakeCon:
        def cursor(self): return FakeCur()
        def close(self): pass

    monkeypatch.setattr(ep.sqlite3, "connect", lambda *a, **k: FakeCon())
    monkeypatch.setattr(ep, "DB", book)   # merely needs to exist
    rc = ep.main()
    return rc, json.loads(book.read_text())


_ENS = {"ensembles": [{
    "id": "ens|momentum|5m", "family": "momentum", "timeframe": "5m",
    "member_ids": ["momentum|BTC/USDT|5m|12|48|1.5"],
}]}


def test_main_refuses_unreachable_geometry(tmp_path, monkeypatch, capsys):
    # Flat BTC 5m series: a +3% target is never reached. The last bar is a
    # NORMAL bar — writing a dipped close here would make the measurement run
    # from below the series (a self-inflicted pass).
    rc, out = _run_main(tmp_path, monkeypatch, _bars([100.0] * 400), _ENS)
    assert rc == 0
    assert out["open"] == [], "an unreachable target must not open a position"
    say = capsys.readouterr().out
    # Say WHY, and do not narrate a ceiling that was never measured.
    assert "GEOMETRY REFUSED" in say and "ens|momentum|5m" in say
    assert "unmeasurable" not in say


def test_main_opens_and_stamps_when_target_is_reachable(tmp_path, monkeypatch):
    # +1% per bar: +3% within 48 bars is reachable.
    rc, out = _run_main(
        tmp_path, monkeypatch, _bars([100.0 * (1.01 ** i) for i in range(400)]), _ENS)
    assert rc == 0
    assert len(out["open"]) == 1
    pos = out["open"][0]
    assert pos["tf_ms"] == 300_000, "bar size must be stamped at entry"
    assert pos["target_pct"] == 3.0
    # The admission carries its measurement and the window it was measured on.
    assert pos["reach"]["p_reach"] is not None and pos["reach"]["p_reach"] >= 5.0
    assert pos["reach"]["measured_from"] and pos["reach"]["measured_to"]
    assert pos["reach_scope"]


def test_main_backfills_tf_ms_on_a_legacy_record_and_times_it_out(tmp_path, monkeypatch):
    # The real pre-2026-10-21 record shape: no tf_ms, hold long since elapsed.
    # It must be closed at TIME on the run that reads it, not carried forever.
    tf_ms, hold = 300_000, 48
    open_ms = 1792598700000
    closes = [100.0] * 200
    rows = _bars(closes, tf_ms=tf_ms, start=open_ms - tf_ms * 150)
    legacy = {"ensemble": "ens|momentum|5m", "family": "momentum", "timeframe": "5m",
              "symbol": "BTC/USDT", "entry": 100.0, "tp": 103.0, "sl": 98.0,
              "rr": 1.5, "hold_bars": hold, "size": 5.0, "open_ms": open_ms,
              "ts_utc": "2026-10-21T16:15:12+00:00", "mode": "paper"}
    rc, out = _run_main(tmp_path, monkeypatch, rows, _ENS,
                        book_state={"open": [legacy], "closed": [], "meta": {}})
    assert rc == 0
    assert out["open"] == []
    assert len(out["closed"]) == 1
    done = out["closed"][0]
    assert done["reason"] == "TIME"
    assert done["tf_ms"] == tf_ms and done["tf_ms_backfilled"]
    assert done["pnl"] == pytest.approx(round(5.0 * (0.0 - ep.COST), 6))


def test_main_does_not_age_a_record_it_cannot_size(tmp_path, monkeypatch, capsys):
    # Negative control for the backfill: no tf_ms, no timeframe, nothing that
    # names a bar size -> left open and reported, never closed on a guess.
    tf_ms, hold = 300_000, 48
    open_ms = 1792598700000
    rows = _bars([100.0] * 200, tf_ms=tf_ms, start=open_ms - tf_ms * 150)
    legacy = {"ensemble": "ens|momentum|5m", "symbol": "BTC/USDT", "entry": 100.0,
              "tp": 103.0, "sl": 98.0, "hold_bars": hold, "size": 5.0,
              "open_ms": open_ms, "mode": "paper"}
    rc, out = _run_main(tmp_path, monkeypatch, rows, _ENS,
                        book_state={"open": [legacy], "closed": [], "meta": {}})
    assert rc == 0
    assert len(out["open"]) == 1 and out["closed"] == []
    assert "UN-AGEABLE OPEN" in capsys.readouterr().out


def test_time_exit_prices_at_the_bar_that_ends_the_hold(tmp_path, monkeypatch):
    # The position was left open for 300 bars though its hold is 48, and the
    # price DRIFTS after bar 48. Exiting at the newest bar (the old behaviour)
    # books bar-300's price; the TIME exit must book bar-48's.
    #
    # This is the case a "rows start at the open" fixture cannot see: the real
    # runner fetches the LAST 200 candles, so a 300-bar-old position has none of
    # its first 100 bars in `rows` and rows[-1] is bar 300.
    tf_ms, hold, open_ms = 300_000, 48, 1792598700000
    # 150 bars of history BEFORE the open, then the position's 48 bars at a flat
    # 100.0 (so bar 0 and bar 48 both close at 100.0), then a drift to 101.0.
    # The runner's 200-bar fetch starts at bar 100, so bar 48 is not in it and
    # the old code would have booked 101.0 for a position due at bar 48.
    closes = [99.0] * 150 + [100.0] * 49 + [101.0] * 251
    rows = _bars(closes, tf_ms=tf_ms, start=open_ms - tf_ms * 150)
    pos = {"ensemble": "ens|momentum|5m", "family": "momentum", "timeframe": "5m",
           "symbol": "BTC/USDT", "entry": 100.0, "tp": 200.0, "sl": 50.0,
           "rr": 1.5, "hold_bars": hold, "tf_ms": tf_ms, "size": 5.0,
           "open_ms": open_ms, "mode": "paper"}
    rc, out = _run_main(tmp_path, monkeypatch, rows, _ENS,
                        book_state={"open": [pos], "closed": [], "meta": {}})
    assert rc == 0 and len(out["closed"]) == 1
    done = out["closed"][0]
    assert done["reason"] == "TIME"
    assert done["exit"] == 100.0, "must exit at the close of bar 48, not bar 300"
    assert done["pnl"] == pytest.approx(round(5.0 * (0.0 - ep.COST), 6))
    # the record says WHICH bar was priced, not just when the scan noticed
    assert done["exit_bar_ms"] == open_ms + hold * tf_ms
    assert done["bars_held"] == pytest.approx(float(hold))
    # idempotent: the 5-minute cadence re-reads the same bars; a closed record
    # must not be closed again (double-booking the P&L)
    rc2, out2 = _run_main(tmp_path, monkeypatch, rows, _ENS, book_state=out)
    assert rc2 == 0 and len(out2["closed"]) == 1 and len(out2["open"]) == 0
