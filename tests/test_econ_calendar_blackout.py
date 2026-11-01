"""Economic-calendar blackout: the window must gate the book it names (2026-11-01).

Two defects, both observed on the live cache:

1. DUPLICATES. The this-week and next-week feeds overlap on boundary days, and
   `fetch_events()` wrote both copies straight to `data/econ-calendar.json`
   (116 rows / 113 unique on 2026-11-01: "Trade Balance" 11-04 08:30 and
   "Unemployment Rate" 11-06 08:30 twice). `risk_state()` iterates the list, so
   a release that arrives twice is read twice. Collapsed before the write.

2. THE WINDOW WAS AIMED AT THE WRONG POPULATION. Every High-impact *USD*
   release opened a T-24h/+6h blackout, but the only caller
   (`bybit_demo_trader`) runs a crypto book holding XAUT + BTC/ETH/SOL; the
   releases that matter to it are the ones that move gold (the same
   `GOLD_MOVERS` set its macro gate is built on). A High USD release that is
   not in that set stopped every BTC/ETH entry for 30 hours for a reason the
   book does not act on.

These tests pin the behaviour on hand-built event lists, never on the live
cache (rule #11: a test that pins tomorrow's data goes red for no defect).
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "econ_calendar", Path(__file__).resolve().parents[1] / "scripts" / "econ_calendar.py"
)
ec = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ec)


def _ev(hours_from_now: float, title: str, impact: str = "High", country: str = "USD"):
    ts = datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
    return {"title": title, "country": country, "impact": impact,
            "ts": ts.timestamp(), "iso": ts.isoformat()}


# ── 1. duplicate collapse ────────────────────────────────────────────────────


def _write_cache(tmp_path, monkeypatch, rows):
    cache = tmp_path / "econ-calendar.json"
    seed = tmp_path / "ff-seed.json"
    monkeypatch.setattr(ec, "CACHE", cache)
    monkeypatch.setattr(ec, "SEED", seed)
    source = [{"title": t, "country": c, "impact": i, "date": iso}
              for (c, i, t, iso) in rows]
    monkeypatch.setattr(ec, "_fetch", lambda url: source if url == ec.FEEDS[0] else [])
    return cache


def test_same_release_from_both_feeds_is_written_once(tmp_path, monkeypatch):
    """this-week + next-week both carry the boundary-day NFP: one row, not two."""
    row = ("USD", "High", "Non-Farm Employment Change", "2026-11-06T03:30:00-05:00")
    cache = _write_cache(tmp_path, monkeypatch, [row, row, row])
    out = ec.fetch_events(force=True)
    assert len(out) == 1
    assert len(__import__("json").loads(cache.read_text())["events"]) == 1


def test_two_real_releases_at_one_minute_both_survive(tmp_path, monkeypatch):
    """Same minute, different titles (NFP + Unemployment Rate) are two events."""
    rows = [("USD", "High", "Non-Farm Employment Change", "2026-11-06T08:30:00+00:00"),
            ("USD", "High", "Unemployment Rate", "2026-11-06T08:30:00+00:00")]
    _write_cache(tmp_path, monkeypatch, rows)
    assert len(ec.fetch_events(force=True)) == 2


def test_same_title_same_minute_different_country_both_survive(tmp_path, monkeypatch):
    rows = [("USD", "High", "Unemployment Rate", "2026-11-06T08:30:00+00:00"),
            ("CAD", "High", "Unemployment Rate", "2026-11-06T08:30:00+00:00")]
    _write_cache(tmp_path, monkeypatch, rows)
    assert len(ec.fetch_events(force=True)) == 2


# ── 2. gold-mover gate ───────────────────────────────────────────────────────


def test_high_usd_release_that_is_not_a_gold_mover_does_not_blackout():
    ev = [_ev(2, "Daylight Saving Time Shift")]      # High impact, no market print
    assert ec.risk_state(events=ev)["blackout"] is False


def test_high_usd_gold_mover_still_blackouts():
    ev = [_ev(2, "Non-Farm Employment Change")]
    st = ec.risk_state(events=ev)
    assert st["blackout"] is True and st["minutes_until"] == 120


def test_high_non_usd_gold_mover_still_ignored():
    ev = [_ev(2, "Unemployment Rate", country="CAD")]
    assert ec.risk_state(events=ev)["blackout"] is False


def test_low_impact_gold_mover_still_ignored():
    ev = [_ev(2, "ADP Non-Farm Employment Change", impact="Low")]
    assert ec.risk_state(events=ev)["blackout"] is False


def test_next_pointer_still_names_a_gated_out_high_release():
    """Gating narrows the blackout, not the 'what's coming' note."""
    ev = [_ev(2, "Daylight Saving Time Shift")]
    st = ec.risk_state(events=ev)
    assert st["blackout"] is False
    assert st["next"] == "Daylight Saving Time Shift"


# ── 3. the fallback seed does not grow into an archive ──────────────────────


def _seed_rows(days_from_now):
    """Seed rows at the given day offsets, in the feed's own date format."""
    out = []
    for d in days_from_now:
        ts = datetime.now(timezone.utc) + timedelta(days=d)
        out.append({"title": f"Event {d:+g}", "country": "USD", "impact": "Low",
                    "date": ts.isoformat()})
    return out


def test_seed_prunes_past_rows_on_merge(tmp_path, monkeypatch):
    """A fallback is what the calendar said last time, not an archive.

    2026-11-01: 874 seed rows over 7 weeks (762 already past) replaced a
    116-event cache the first time a fetch was blocked. Old past rows can be
    neither upcoming nor inside a blackout, so they must go.
    """
    seed = tmp_path / "ff-seed.json"
    seed.write_text(__import__("json").dumps(_seed_rows([-40, -10, -0.5, 3, 60])))
    monkeypatch.setattr(ec, "SEED", seed)
    ec._save_seed([])
    kept = sorted(e["title"] for e in __import__("json").loads(seed.read_text()))
    assert kept == ["Event +3", "Event -0.5"]


def test_blocked_fetch_falls_back_to_a_pruned_seed(tmp_path, monkeypatch):
    """Network blocked + week-old seed: the cache gets the seed, not history."""
    seed = tmp_path / "ff-seed.json"
    seed.write_text(__import__("json").dumps(_seed_rows([-30, 2, 5])))
    cache = tmp_path / "econ-calendar.json"
    monkeypatch.setattr(ec, "SEED", seed)
    monkeypatch.setattr(ec, "CACHE", cache)
    monkeypatch.setattr(ec, "_fetch", lambda url: [])
    out = ec.fetch_events(force=True)
    assert sorted(e["title"] for e in out) == ["Event +2", "Event +5"]
