"""T0.19 — the ensemble book stopped opening and nothing said so.

Measured 2026-11-10 from `logs/ensemble-paper.log` (10,766 runs, 10-04 -> 11-10):

  * 5,181 `GEOMETRY REFUSED` lines since 2026-10-22T01:05 (momentum|5m, then
    momentum|30m from 15:15) — the same three lines, once per 5-minute run;
  * last open 2026-11-05T05:00:53, then **1,538 runs / 6.4 days with opened=0**,
    1,336 of them refusing;
  * the wrapper prints nothing when a run opens and closes nothing, so a lane
    that refuses every candidate and a lane with no candidate look identical;
  * the book itself: 205 closed, -8.34 net, 18.5% WR, **TP 0 / SL 57 / TIME 148**.

Re-measured on real BTC candles the same day: P(reach +3% in 48 bars of 5m) =
1.4%, P(reach +3% in 12 bars of 30m) = 1.71%. Both under the 5% floor. So the
gate is CORRECT and the lane is dead by construction: every ensemble sizes its
target as 2% * rr, `rr` is not a knob, and a refused geometry is refused forever.

What is pinned here (the fix is REPORTING ONLY — MIN_P_MARGIN and the targets are
untouched, because re-sizing is a strategy change and an owner call):

  1. an unchanged refusal is not reprinted every run; a CHANGED one is at once;
  2. the streak is counted in runs the runner actually completed, and the card
     says how long and (by elimination) why;
  3. a fired-but-refused run, a quiet run and a changed-`rr` run are told apart —
     the negative controls matter most: a gate that screams on a healthy book is
     a worse alarm than the silence it replaced.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path("/opt/trading-agent")
spec = importlib.util.spec_from_file_location(
    "ensemble_paper_t019", ROOT / "scripts/ensemble_paper.py")
assert spec is not None and spec.loader is not None
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)


def _bars(closes, tf_ms=300_000, start=0):
    return [(start + i * tf_ms, c, c, c, c) for i, c in enumerate(closes)]


# Flat: a +3% target is never reached over any hold -> the gate refuses.
_FLAT = _bars([100.0] * 400)


def _run(tmp_path, monkeypatch, rows, ens, book_state=None, capsys=None):
    book = tmp_path / "book.json"
    book.write_text(json.dumps(book_state or {"open": [], "closed": [], "meta": {}}))
    (tmp_path / "ensembles.json").write_text(json.dumps(ens))
    monkeypatch.setattr(ep, "BOOK", book)
    monkeypatch.setattr(ep, "ENSEMBLES", tmp_path / "ensembles.json")
    monkeypatch.setattr(ep, "_candles", lambda cur, sym, tf, limit: rows[-max(limit, 5):])
    monkeypatch.setattr(ep, "_rule_fires", lambda *a, **k: True)
    monkeypatch.setattr(ep, "long_window_verdict",
                        lambda *a, **k: {"tested": False, "reason": "fixture"})

    class FakeCur:
        connection = None

    class FakeCon:
        def cursor(self): return FakeCur()
        def close(self): pass

    monkeypatch.setattr(ep.sqlite3, "connect", lambda *a, **k: FakeCon())
    monkeypatch.setattr(ep, "DB", book)
    rc = ep.main()
    out = capsys.readouterr().out if capsys else ""
    return rc, json.loads(book.read_text()), out


_ENS = {"ensembles": [{
    "id": "ens|momentum|5m", "family": "momentum", "timeframe": "5m",
    "member_ids": ["momentum|BTC/USDT|5m|12|48|1.5"],
}]}


# ── 1. the refusal is news once, state after that ──────────────────────────

def test_an_unchanged_refusal_is_printed_once_then_suppressed(tmp_path, monkeypatch, capsys):
    rc, st1, say1 = _run(tmp_path, monkeypatch, _FLAT, _ENS, capsys=capsys)
    assert rc == 0 and st1["open"] == []
    assert "GEOMETRY REFUSED" in say1, "the first refusal must be reported"
    # meta now carries what was refused, so the next run can compare.
    assert st1["meta"]["refusals"]["ens|momentum|5m"]

    rc, st2, say2 = _run(tmp_path, monkeypatch, _FLAT, _ENS,
                         book_state=st1, capsys=capsys)
    assert st2["meta"]["refusals"] == st1["meta"]["refusals"]
    assert "GEOMETRY REFUSED" not in say2, (
        "the same verdict every 5 minutes is how an alarm stops being read")
    # The suppressed line is carried by the streak instead — but only past
    # DEAD_LANE_RUNS, so two runs into a fix it must stay quiet.
    assert "NO OPEN IN" not in say2
    assert st2["meta"]["silent_runs"] == 2


def test_a_changed_refusal_prints_immediately(tmp_path, monkeypatch, capsys):
    rc, st1, _ = _run(tmp_path, monkeypatch, _FLAT, _ENS, capsys=capsys)
    assert st1["meta"]["refusals"]
    # A different member (rr 2.0 -> +4% target) is a DIFFERENT verdict and must
    # be reported even though the previous run also refused this ensemble.
    ens2 = {"ensembles": [{
        "id": "ens|momentum|5m", "family": "momentum", "timeframe": "5m",
        "member_ids": ["momentum|BTC/USDT|5m|12|48|2.0"],
    }]}
    rc, st2, say2 = _run(tmp_path, monkeypatch, _FLAT, ens2,
                         book_state=st1, capsys=capsys)
    assert "GEOMETRY REFUSED" in say2 and "target +4.0%" in say2
    assert st2["meta"]["refusals"]["ens|momentum|5m"] != \
        st1["meta"]["refusals"]["ens|momentum|5m"]


def test_a_clean_first_run_after_the_fix_still_prints_its_refusal(tmp_path, monkeypatch, capsys):
    # The live book's meta has NO `refusals` key: the first run under this code
    # must not "recognise" a repeat it never saw.
    legacy = {"open": [], "closed": [], "meta": {
        "created_utc": "2026-10-04T04:07:18+00:00", "opens_total": 205,
        "closes_total": 205, "last_run_utc": "2026-11-10T13:10:47+00:00"}}
    rc, st, say = _run(tmp_path, monkeypatch, _FLAT, _ENS,
                       book_state=legacy, capsys=capsys)
    assert "GEOMETRY REFUSED" in say


# ── 2. the streak and its cause ────────────────────────────────────────────

def test_the_streak_counts_runs_and_names_the_last_open(tmp_path, monkeypatch, capsys):
    st = {"open": [], "closed": [], "meta": {
        "silent_runs": ep.DEAD_LANE_RUNS - 1,
        "last_open_utc": "2026-11-05T05:00:53+00:00",
        "refusals": {"ens|momentum|5m": "3.0|0.5|widened window"}}}
    rc, st2, say = _run(tmp_path, monkeypatch, _FLAT, _ENS,
                        book_state=st, capsys=capsys)
    assert st2["meta"]["silent_runs"] == ep.DEAD_LANE_RUNS
    assert f"NO OPEN IN {ep.DEAD_LANE_RUNS} RUNS" in say
    assert "2026-11-05T05:00:53Z" in say
    assert "REFUSED on geometry" in say, "the cause must be attributed, not implied"
    assert "ens|momentum|5m" in say.split("cause:")[1]
    assert "owner call" in say, "the reader must know this is not a runner bug"
    assert "LAST record this book made" in say


def test_a_quiet_book_below_the_streak_says_nothing(tmp_path, monkeypatch, capsys):
    # NEGATIVE CONTROL. A book that simply has no signal must not be declared
    # dead at run 2 — the streak is what separates "quiet" from "dead".
    st = {"open": [], "closed": [], "meta": {"silent_runs": 2,
                                             "last_open_utc": "2026-11-10T12:00:00+00:00"}}
    rc, st2, say = _run_quiet(tmp_path, monkeypatch, _ENS, st, capsys)
    assert st2["meta"]["silent_runs"] == 3
    assert "NO OPEN IN" not in say


def _run_quiet(tmp_path, monkeypatch, ens, book_state, capsys):
    book = tmp_path / "book.json"
    book.write_text(json.dumps(book_state))
    (tmp_path / "ensembles.json").write_text(json.dumps(ens))
    monkeypatch.setattr(ep, "BOOK", book)
    monkeypatch.setattr(ep, "ENSEMBLES", tmp_path / "ensembles.json")
    monkeypatch.setattr(ep, "_candles", lambda cur, sym, tf, limit: _FLAT[-60:])
    monkeypatch.setattr(ep, "_rule_fires", lambda *a, **k: False)

    class FakeCur:
        connection = None

    class FakeCon:
        def cursor(self): return FakeCur()
        def close(self): pass

    monkeypatch.setattr(ep.sqlite3, "connect", lambda *a, **k: FakeCon())
    monkeypatch.setattr(ep, "DB", book)
    rc = ep.main()
    return rc, json.loads(book.read_text()), capsys.readouterr().out


def test_a_quiet_book_past_the_streak_is_called_quiet_not_proven_dead(tmp_path, monkeypatch, capsys):
    st = {"open": [], "closed": [], "meta": {
        "silent_runs": ep.DEAD_LANE_RUNS,
        "last_open_utc": "2026-11-05T05:00:53+00:00",
        "refusals": {"ens|momentum|5m": "3.0|0.5|widened window"}}}
    rc, st2, say = _run_quiet(tmp_path, monkeypatch, _ENS, st, capsys)
    assert "NO OPEN IN" in say, "a long silence is still reported"
    assert "no candidate has fired" in say
    assert "REFUSED on geometry" not in say, "do not blame a gate that never fired"
    # NEGATIVE CONTROL: the meta still holds a refusal from a run whose rule DID
    # fire. Blaming it here would be the stale-record defect this repo keeps
    # finding — the cause is read from THIS run's scan, not from the file.


def test_a_changed_rr_is_reported_as_a_writer_change_not_a_refusal(tmp_path, monkeypatch, capsys):
    st = {"open": [], "closed": [], "meta": {
        "silent_runs": ep.DEAD_LANE_RUNS,
        "last_open_utc": "2026-11-05T05:00:53+00:00",
        "rr_by_ensemble": {"ens|momentum|5m": 1.5}}}
    ens2 = {"ensembles": [{
        "id": "ens|momentum|5m", "family": "momentum", "timeframe": "5m",
        "member_ids": ["momentum|BTC/USDT|5m|12|48|1.0"],
    }]}
    rc, st2, say = _run_quiet(tmp_path, monkeypatch, ens2, st, capsys)
    assert "GEOMETRY CHANGED" in say and "ens|momentum|5m" in say
    # THE POINT: rr_by_ensemble must NOT be overwritten while the book is not
    # opening, or the writer change disappears on the very next run.
    assert st2["meta"]["rr_by_ensemble"] == {"ens|momentum|5m": 1.5}


# ── 3. an open resets the streak and stamps the geometry ───────────────────

def test_an_open_resets_the_streak_and_stamps_rr(tmp_path, monkeypatch, capsys):
    rising = _bars([100.0 * (1.01 ** i) for i in range(400)])
    st = {"open": [], "closed": [], "meta": {"silent_runs": 500,
                                             "last_open_utc": "2026-10-01T00:00:00+00:00"}}
    rc, st2, say = _run(tmp_path, monkeypatch, rising, _ENS,
                        book_state=st, capsys=capsys)
    assert len(st2["open"]) == 1
    assert st2["meta"]["silent_runs"] == 0
    assert st2["meta"]["rr_by_ensemble"] == {"ens|momentum|5m": 1.5}
    assert "NO OPEN IN" not in say


def test_the_lane_silence_helper_is_pure(tmp_path):
    # The card's numbers come from the helper, so it must not depend on `now`
    # being real time in the test.
    import datetime as dt
    st = {"meta": {"silent_runs": 30, "last_open_utc": "2026-11-05T05:00:53+00:00"}}
    out = ep._lane_silence(st, _ENS, now=dt.datetime(2026, 11, 10, 13, 0,
                                                     tzinfo=dt.timezone.utc),
                           fired={"ens|momentum|5m"},
                           refused_ids={"ens|momentum|5m"})
    assert out["dead"] is True and out["runs"] == 30
    assert "(5.3d)" in out["last_open"], out["last_open"]


def test_live_book_measured_numbers_are_what_the_card_will_say():
    """Not a fixture: the real book's own history, so the first live run after
    this ships cannot quietly disagree with the measurement in the comment."""
    book = json.loads((ROOT / "data/ensemble-paper.json").read_text())
    closed = book["closed"]
    pnl = sum(p.get("pnl", 0) for p in closed)
    assert len(closed) >= 205
    assert pnl < 0
    assert book["meta"].get("last_run_utc"), "the runner is alive; it opens nothing"


# ── 4. a book with no stamp has NOT "just opened" ───────────────────────────
# Found on the FIRST live run after this code landed: the card printed
#   NO OPEN IN 24 RUNS (last open 2026-11-10T13:15:47Z (0.1d))
# while opens_total was 205 and the newest close was 2026-11-05. The streak said
# 24 runs, the age said 0.1d — two different events, one line. Cause:
#   `if opened or not st["meta"].get("last_open_utc")` -- true on the first run
#   after deploy, so it stamped the CLOCK even though nothing opened.
# A streak counter and a "last happened" clock must agree; seeding the clock from
# the clock guarantees they never can on the run that introduces the field.

def test_no_stamp_is_seeded_from_the_book_not_from_the_clock(tmp_path, monkeypatch, capsys):
    st = {"open": [], "closed": [
        {"ensemble": "ens|momentum|5m", "open_ms": 1793779200000,   # 2026-11-04T08:00Z
         "ts_utc": "2026-11-04T12:15:31+00:00", "pnl": -0.1},
        {"ensemble": "ens|momentum|5m", "open_ms": 1793817600000,   # 2026-11-04T18:40Z
         "ts_utc": "2026-11-04T18:40:00+00:00", "pnl": -0.1},
    ], "meta": {"silent_runs": ep.DEAD_LANE_RUNS - 1}}
    rc, st2, say = _run_quiet(tmp_path, monkeypatch, _ENS, st, capsys)
    assert ep.DEAD_LANE_RUNS - 1 + 1 == st2["meta"]["silent_runs"]
    seeded = st2["meta"]["last_open_utc"]
    # 2026-11-04T18:40Z, from open_ms of the newest record — NOT today.
    assert seeded.startswith("2026-11-04T18:40"), seeded
    assert "NO OPEN IN" in say
    assert "2026-11-04" in say, "the card's age must come from the book"
    assert say.count("NO OPEN IN") == 1


def test_an_empty_book_gets_no_invented_open_time(tmp_path, monkeypatch, capsys):
    # No closes, no prior stamp: there is no evidence of an open, so the field
    # stays absent and the helper says so, instead of claiming one just happened.
    st = {"open": [], "closed": [], "meta": {"silent_runs": ep.DEAD_LANE_RUNS}}
    rc, st2, say = _run_quiet(tmp_path, monkeypatch, _ENS, st, capsys)
    assert "last_open_utc" not in st2["meta"], st2["meta"]
    assert "never recorded" in say


def test_the_book_seed_prefers_open_ms_and_ignores_garbage():
    assert ep._last_book_time([]) is None
    assert ep._last_book_time([{"ts_utc": "2026-11-01T00:00:00+00:00"}]).startswith("2026-11-01")
    got = ep._last_book_time([
        {"open_ms": "not-an-int"},
        {"open_ms": 1793779200000},
        {"open_ms": 1793817600000},
    ])
    assert got.startswith("2026-11-04T18:40"), got
