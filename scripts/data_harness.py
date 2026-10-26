#!/usr/bin/env python3
"""DATA HARNESS — a single manifest + freshness gate for EVERY data source.

WHY THIS EXISTS: we had a health checker, but it only watched the SQLite
candle store (3 symbols). The data we ACTUALLY backtest and decide on -
the research price caches, the alt-data, the trade journal, FRED, the
sentiment feed, the champion config - was UNMONITORED.

That is the dangerous kind of gap: if research/price-cache-1000d.json goes
stale, every backtest silently tests old data and NOBODY NOTICES. Silent
staleness is the #1 data failure, and it produces confident wrong answers.

So: one manifest, one verdict per source, alerts only on problems.

Verdicts:
  OK       fresh and adequately sized
  STALE    older than its expected refresh interval  <- the dangerous one
  THIN     present but too few rows/symbols/days
  MISSING  does not exist
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
D = BASE / "data"
RES = BASE / "research"

# name, path, kind, max_age_hours, min_rows, what it feeds
MANIFEST = [
    ("research price cache (1000d)", RES / "price-cache-1000d.json",
     "json_map", 36, 24, "every backtest + walk-forward"),
    ("gold price cache (5y)", RES / "price-cache-gold-5y.json",
     "json_map", 36, 1, "gold leg + correlation study"),
    ("labelled setups", RES / "labelled-setups.json",
     "json_list", 200, 1000, "ML / selectivity research"),
    ("F&G history", RES / "fng.json",
     "json_map", 36, 1000, "sentiment feature"),
    ("alt-data (OI/LS/taker)", D / "altdata.jsonl",
     "jsonl", 30, 60, "the 30-day-limited positioning columns"),
    ("trade journal", D / "regime-trades.jsonl",
     "jsonl", 120, 1, "THE dataset - our own labelled outcomes"),
    ("champion config", D / "champion.json",
     "json", 200, 1, "live trader config"),
    ("research registry", D / "research-registry.json",
     "json", 2000, 1, "failure memory"),
    ("econ calendar", D / "econ-calendar.json",
     "json", 72, 1, "event blackouts"),
    ("macro cache", D / "macro-cache.json",
     "json", 48, 1, "regime confirmation"),
    ("regime state", D / "regime-state.json",
     "json", 12, 1, "live positions"),
    ("candle store (sqlite)", D / "trading.db",
     "sqlite", 6, 1000, "real-time ingestion"),
    ("FRED macro series", D / "fred",
     "dir_csv", 240, 3, "real yields, dollar index"),
]


# some sources are EXPECTED to be young. A dataset that started three days
# ago having 30 rows is not a failure - it is the normal state of a new
# dataset. Flagging it as a problem would train us to ignore the harness.
# After the grace window we start judging it properly.
GRACE_DAYS = {
    "alt-data (OI/LS/taker)": 7,
    "trade journal": 14,
    "champion config": 3,
}


def age_hours(path: Path) -> float:
    return (time.time() - path.stat().st_mtime) / 3600.0


# A sqlite "store" is a set of SERIES, and its age is the age of the OLDEST
# series still being written — not the file's mtime and not MAX(open_time)
# over the table. Measured 2026-10-25 08:00 UTC: this harness said
# `candle store (sqlite) OK 0.0h 10 symbols` while seven of the ten symbols
# had had no 5m bar for 5.9h, because the file mtime and the table MAX were
# both set by BTC. Same defect as the coverage gate in
# trading.data.health (2026-10-24), still open one monitor over — and this is
# the monitor that runs every day.
SERIES_LAG_ALARM_H = 1.0   # ingest runs every 15 min; 1h means four missed runs


def sqlite_series_age(db_path: Path):
    """(age_h, note) for a candle sqlite store, judged per SERIES.

    age_h = hours since the newest closed 5m bar of the LAST symbol to have
    one — i.e. the age of the darkest series that is still nominally fed.
    A symbol that has never had a 5m bar is counted separately (`no 5m`),
    because "absent" and "stale" are different faults.
    """
    import sqlite3
    now_ms = int(time.time() * 1000)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = list(con.execute(
            "SELECT symbol, MAX(open_time_utc) FROM candles "
            "WHERE timeframe='5m' GROUP BY symbol"))
        n_sym = con.execute(
            "SELECT COUNT(DISTINCT symbol) FROM candles").fetchone()[0]
    finally:
        con.close()
    if not rows:
        return 0.0, "empty store"
    # sort on AGE ONLY: (lag, symbol) tuples break ties on the ticker, and
    # four symbols seeded on the same tick all have the same age — the "worst
    # symbol" came out as whichever name sorts last (SOL), not the one whose
    # feed actually died. Ties are reported as a set.
    lags = [(now_ms - mx) / 3600e3 for _s, mx in rows]
    names = [s for s, _mx in rows]
    worst_lag = max(lags)
    worst_syms = [n for lag, n in zip(lags, names) if lag == worst_lag]
    note = f"{n_sym} symbols"
    dark = [n for lag, n in zip(lags, names) if lag > SERIES_LAG_ALARM_H]
    if dark:
        worst = "/".join(sorted(n.split("/")[0] for n in worst_syms)[:4])
        more = "…" if len(worst_syms) > 4 else ""
        note = (f"{n_sym} symbols | {len(dark)} stale >{SERIES_LAG_ALARM_H:.0f}h "
                f"(worst {worst}{more} {worst_lag:.1f}h)")
        return worst_lag, note
    return worst_lag, f"{note} | all series < {SERIES_LAG_ALARM_H:.0f}h"


def count_rows(path: Path, kind: str):
    """returns (rows, extra-note). never raises."""
    try:
        if kind == "jsonl":
            n = sum(1 for line in path.read_text().splitlines() if line.strip())
            return n, ""
        if kind == "json_map":
            d = json.loads(path.read_text())
            if isinstance(d, dict):
                # series of day -> value, or symbol -> series
                first = next(iter(d.values())) if d else None
                if isinstance(first, dict):
                    return len(d), f"{len(d)} symbols x ~{len(first)} days"
                return len(d), f"{len(d)} days"
            return len(d), ""
        if kind == "json_list":
            d = json.loads(path.read_text())
            return (len(d) if isinstance(d, list) else 1), ""
        if kind == "json":
            # plain JSON object - count meaningful keys, not lines
            d = json.loads(path.read_text())
            if isinstance(d, dict):
                # prefer a nested collection IF IT IS NON-EMPTY. An empty
                # list is not evidence of content (champion.json starts with
                # promotions: [] and that is a healthy state, not a problem).
                for k in ("entries", "promotions", "rows", "real", "positions"):
                    v = d.get(k)
                    if isinstance(v, list) and len(v) > 0:
                        return len(v), f"{len(d)} keys ({k}: {len(v)})"
                return len(d), f"{len(d)} keys"
            if isinstance(d, list):
                return len(d), ""
            return 1, ""
        if kind == "dir_csv":
            fs = list(path.glob("*.csv"))
            return len(fs), ", ".join(f.stem for f in fs[:4])
        if kind == "sqlite":
            import sqlite3
            c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            n = c.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
            s = c.execute(
                "SELECT COUNT(DISTINCT symbol) FROM candles").fetchone()[0]
            return n, f"{s} symbols"
        return 0, ""
    except Exception as e:
        return -1, f"read error: {str(e)[:40]}"


def check():
    print("=" * 100)
    print(f"DATA HARNESS — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    print("=" * 100)
    print(f"{'source':30} {'verdict':8} {'age':>9} {'rows':>9}  notes")
    print("-" * 100)

    problems = []
    rows_report = []
    for name, path, kind, max_age, min_rows, feeds in MANIFEST:
        if not path.exists():
            # some files legitimately do not exist yet (the trade journal is
            # created by the first closed trade). Report PENDING, not a fault,
            # so real problems stay visible.
            pending = name in ("trade journal",)
            verdict = "PENDING" if pending else "MISSING"
            print(f"{name[:30]:30} {verdict:8} {'-':>9} {'-':>9}  feeds: {feeds}")
            if not pending:
                problems.append((name, "MISSING", f"expected at {path}"))
            rows_report.append({"name": name, "verdict": verdict,
                                "feeds": feeds})
            continue

        age = age_hours(path)
        rows, note = count_rows(path, kind)
        if kind == "sqlite" and path.exists():
            # the store's age is its DARKEST series, not the file's mtime
            age, series_note = sqlite_series_age(path)
            note = f"{note} | {series_note}"

        if rows >= 0 and rows < min_rows:
            verdict = "THIN"
        elif age > max_age:
            verdict = "STALE"
        elif rows < 0:
            verdict = "ERROR"
        else:
            verdict = "OK"

        # young dataset => THIN is expected, not a problem
        grace = GRACE_DAYS.get(name, 0)
        if verdict == "THIN" and grace:
            try:
                created = time.time() - path.stat().st_ctime
                if created / 86400.0 <= grace:
                    verdict = "YOUNG"
            except Exception:
                pass

        age_s = f"{age:.1f}h" if age < 48 else f"{age/24:.0f}d"
        rows_s = str(rows) if rows >= 0 else "err"
        # 26 chars cuts off exactly the part that says WHICH series is dark
        # ("4 symbols | 1 stale >1h (worst ADA 10.1h)" is 40), and a report
        # that says "something is stale" without naming it is the defect this
        # change is about. 60 fits a terminal line and keeps the worst symbol.
        print(f"{name[:30]:30} {verdict:8} {age_s:>9} {rows_s:>9}  "
              f"{note[:60]} | {feeds}")

        # STALE / THIN / MISSING / ERROR are faults. YOUNG and PENDING are
        # legitimate states, not problems - counting them as faults is how a
        # monitor trains you to ignore it.
        if verdict in ("STALE", "THIN", "MISSING", "ERROR"):
            problems.append((name, verdict,
                             f"age {age_s} (max {max_age}h), rows {rows} "
                             f"(min {min_rows})"))
        rows_report.append({"name": name, "verdict": verdict, "age_h": round(age, 1),
                            "rows": rows, "feeds": feeds})

    print("-" * 100)
    ok = sum(1 for r in rows_report if r["verdict"] == "OK")
    print(f"  {ok}/{len(MANIFEST)} sources OK")

    if problems:
        print()
        print("  ⚠️ PROBLEMS:")
        for n, v, why in problems:
            print(f"    [{v}] {n}: {why}")
    else:
        print("  ✅ all sources fresh and adequately sized")

    # persist a manifest snapshot so drift is visible over time
    snap = D / "data-manifest.json"
    try:
        prev = json.loads(snap.read_text()) if snap.exists() else None
        json.dump({"checked": datetime.now(timezone.utc).isoformat(),
                   "sources": rows_report,
                   "problems": [p[0] for p in problems]}, snap.open("w"), indent=1)
        if prev:
            was = set(prev.get("problems", []))
            now = set(p[0] for p in problems)
            newly = now - was
            fixed = was - now
            if newly:
                print(f"  🆕 NEW problem(s): {', '.join(newly)}")
            if fixed:
                print(f"  ✅ resolved: {', '.join(fixed)}")
    except Exception:
        pass

    return problems


if __name__ == "__main__":
    try:
        p = check()
        raise SystemExit(1 if p else 0)
    except SystemExit:
        raise
    except Exception as e:
        print(f"data harness error: {str(e)[:200]}")
