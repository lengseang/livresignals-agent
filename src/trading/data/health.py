"""Data health monitor: freshness, gaps, storage checks, alert hooks.

Used by P1-03 daily soak. Produces reports/data-health.json and returns
nonzero on a data-health failure (for cron/CI integration).

⚠️ Two ways this report has lied, both now guarded (2026-10-20):

1. `check_data_health(db_path=None)` wrote its result to the GLOBAL
   `reports/data-health.json` even when `db_path` was a temp DB — the unit
   tests overwrote the live report with a verdict about an empty database.
   `publish=True` (CLI only) writes the report; test/caller runs do not.
2. `ok` is computed from the candles table alone, and a WAL-mode read-only
   connection can miss the writer's uncommitted WAL frame. Report BOTH:
   `ok` (candle health) and `ok_all` (candles + sqlite storage: mode, WAL
   visibility, write/delete probe). A monitor may not claim health while
   blind to part of its own instrument.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from trading.config import (FRESHNESS_DEADLINES, FRESHNESS_MARGIN_S,
                            INGEST_CADENCE_S, REPORTS_DIR, SYMBOLS, TIMEFRAMES,
                            SQLITE_PATH)
from trading.data.store import storage_check


# A series is judged against the freshest series IN THE STORE of the same
# timeframe, not against wall-clock. Two reasons, both measured 2026-10-24:
#   1. the store holds symbols outside config.SYMBOLS, and most ingest paths
#      use fetch_ohlcv(limit=N), which only reaches back N bars — a series
#      that has not been *re-ingested* for 20h looks stale by the clock while
#      every symbol still has contiguous data (the 5m clock said 256 bars
#      "missing" for 7 symbols when the real cause was 34,441 rows written
#      into one 20-minute window at 03:14 by a manual backfill).
#   2. wall-clock staleness is already covered by the per-series check against
#      FRESHNESS_DEADLINES, which only inspects config.SYMBOLS.
# So: an ingested symbol is COVERED when both clocks agree it is fine, and the
# verdict names which clock failed.
MIN_BARS_IN_WINDOW_ALIVE = 10   # of 12 slots/hour — "this series is trading"
# The RECENT view. Wide enough that a 15-min ingest gap cannot create a hole,
# narrow enough that ingest stopping right now shows up on the next run.
HOLE_WINDOW_HOURS = 3
IV_5M = 300_000          # ms in one 5m bar
# A bar that closed this long ago SHOULD be in the store. Ingest runs every
# 15 min and writes the bar that closed on the previous tick, so the bar
# immediately before "now" is legitimately absent for up to 15 min. Derived
# from the same ingest cadence the per-series deadlines use, not hand-picked.
INGEST_SLACK_MS = (INGEST_CADENCE_S + FRESHNESS_MARGIN_S) * 1000


def series_coverage(db_path: Path | None = None, window_hours: int = 1,
                    hole_window_h: int = HOLE_WINDOW_HOURS,
                    grace_h: float = 0.0) -> dict:
    """WHO does the store actually cover, and is the coverage current?

    The store-level freshness verdict (`ok`) asks "is the newest candle in the
    table recent?" — which BTC alone can answer yes to while every alt stopped
    hours ago. A symbol missing from the store is not "unmeasured" to every
    replay/calibrate/learn script that reads it: it is invisible, and the book
    silently scores itself on the subset it happens to have.

    Returns one entry per symbol actually present in the store:
      bars_win     closed 5m bars present in the last `window_hours`
      latest_any   newest close across every timeframe
      cover_lag_h  hours between latest_any and the store-wide latest_any
      holes        5m slots missing in the last `hole_window_h`, excluding any
                   that ingest has not had a turn to fill yet
      covered      bool — present on EVERY timeframe AND (unless the symbol is
                   within `grace_h` of the store's newest data) at least
                   MIN_BARS_IN_WINDOW_ALIVE bars in the window with no holes
      why          the single reason it is not covered ('' when covered)
    """
    con = sqlite3.connect(f"file:{db_path or SQLITE_PATH}?mode=ro", uri=True)
    try:
        now_ms = int(time.time() * 1000)
        latest_any = {}
        for sym, close in con.execute(
                "SELECT symbol, MAX(close_time_utc) FROM candles GROUP BY symbol"):
            latest_any[sym] = close
        # timeframes a symbol has EVER been written on — a symbol that is dense
        # on 5m but has never had a 4h bar is not covered for a 4h replay
        tfs_of: dict[str, set] = {}
        for sym, tf in con.execute("SELECT DISTINCT symbol, timeframe FROM candles"):
            tfs_of.setdefault(sym, set()).add(tf)
        win_ms = window_hours * 3600 * 1000
        # 5m GAPS, not just the newest bar: a series that sat un-refreshed for
        # 40 minutes and then got one bar back is "fresh" by the clock and can
        # pass a count, while every consumer of it is missing the 8 bars in
        # between. On the real store a series fetched 20h ago with limit=900
        # still has a bar in most 5-minute slots, so a HOLE is a real hole.
        # The 5m grid is anchored on the newest CLOSED bar, not on wall-clock:
        # the current bar is still forming and its open is not on the grid yet.
        # Two windows, two thresholds. `hole_window_h` is the RECENT view
        # judged strictly (a hole means ingest stopped now); `window_hours` is
        # the broader view that just proves the series is alive.
        grid_end = (now_ms // IV_5M) * IV_5M - IV_5M
        win_opens = {grid_end - k * IV_5M
                     for k in range(window_hours * 12)}
        hole_opens = {grid_end - k * IV_5M
                      for k in range(hole_window_h * 12)}
        have = {}
        for sym, o in con.execute(
                "SELECT symbol, open_time_utc FROM candles WHERE timeframe='5m' "
                "AND open_time_utc > ?", (min(win_opens | hole_opens),)):
            have.setdefault(sym, set()).add(o)
        store_latest = max(latest_any.values()) if latest_any else 0
        out = {}
        for sym, close in latest_any.items():
            lag_h = (store_latest - close) / 3600e3
            bars_win = len(win_opens & have.get(sym, set()))
            missing_tf = sorted(set(TIMEFRAMES) - tfs_of.get(sym, set()))
            # the newest slot's bar may still be open — never count it missing.
            # Not `<= now - 1 bar`: right after a boundary the previous bar is
            # not ingested YET (ingest runs every 15 min), so "closed" is not
            # "expected here". A hole only counts once ingest has had a turn.
            holes = sorted(o for o in hole_opens
                           if o not in have.get(sym, set())
                           and o < grid_end - INGEST_SLACK_MS)
            if missing_tf:
                why = f"no {'/'.join(missing_tf)} series at all"
            elif bars_win >= MIN_BARS_IN_WINDOW_ALIVE and not holes:
                why = ""
            elif lag_h <= grace_h:
                why = ""      # low-frequency symbol, refreshed with the store
            elif bars_win == 0:
                # NOTE: `lag_h` is NOT evidence of freshness. A dead 5m feed
                # whose 1d bars still land is only 1h "behind the store" by
                # max-close (measured: 0.9h for a 21h-dead 5m series). Only
                # bars inside the window count; the lag is reported beside.
                why = f"0 bars in {window_hours}h, {lag_h:.1f}h behind the store"
            elif holes:
                why = (f"{len(holes)} holes in {hole_window_h}h "
                       f"(first {time.strftime('%H:%M', time.gmtime(holes[0] / 1000))}Z), "
                       f"{lag_h:.1f}h behind the store")
            else:
                why = (f"only {bars_win}/{MIN_BARS_IN_WINDOW_ALIVE * window_hours} "
                       f"bars in {window_hours}h, {lag_h:.1f}h behind the store")
            out[sym] = {"bars_win": bars_win, "latest_any": close,
                        "holes": len(holes),
                        "cover_lag_h": round(lag_h, 1),
                        "covered": why == "", "why": why}
        return out
    finally:
        con.close()


def check_data_health(db_path: Path | None = None, publish: bool = False,
                      coverage: bool = True) -> dict:
    con = sqlite3.connect(str(db_path or SQLITE_PATH))
    con.row_factory = sqlite3.Row
    now_ms = int(time.time() * 1000)
    problems = []
    detail = {}

    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            row = con.execute(
                "SELECT MAX(close_time_utc) AS max_close, MAX(open_time_utc) AS latest, COUNT(*) AS n "
                "FROM candles WHERE symbol=? AND timeframe=?",
                (symbol, tf),
            ).fetchone()
            latest = row["latest"]
            max_close = row["max_close"]
            n = row["n"]
            key = f"{symbol}:{tf}"
            if latest is None:
                problems.append(f"{key}: no data")
                detail[key] = {"n": 0, "fresh": False, "gap": None}
                continue
            deadline_ms = FRESHNESS_DEADLINES[tf] * 1000
            # Freshness is measured against the latest CLOSE time: a candle is
            # current if its interval end is within the deadline of now.
            fresh = (now_ms - max_close) <= deadline_ms
            if not fresh:
                problems.append(f"{key}: stale (max_close {max_close} now {now_ms})")
            # gap check: expected next open = max_close + 1
            gap = None
            expected_next_open = max_close + 1
            if latest > expected_next_open:
                gap = {"expected": expected_next_open, "actual": latest}
                problems.append(f"{key}: gap detected {gap}")
            detail[key] = {"n": n, "fresh": fresh, "latest_open": latest, "max_close": max_close, "gap": gap}

    # The candle verdict is exactly the set of candle problems collected above.
    # Derive it instead of setting a flag per branch: the flag version shipped
    # once with `candles_ok = True` never went False, so a stale/empty store
    # reported ok. Every append to `problems` above is a candle problem.
    candles_ok = not problems

    # COVERAGE (added 2026-10-24): the per-series loop above only walks
    # config.SYMBOLS. On 2026-10-23 the store carried 10 symbols, 7 of them
    # 21h stale, and this function said `ok: true, problems: []` because the
    # three core symbols were current — while 43.7% of the paper book traded
    # symbols the store had never seen. Freshness of the newest row in a
    # table is not freshness of the table. `ok` keeps its candle-only meaning
    # (the heartbeat depends on it); `coverage` is a separate, stated verdict,
    # and `ok_all` requires both — the same rule as storage above.
    cov = None
    if coverage:
        cov = series_coverage(db_path)
        for sym, c in cov.items():
            if not c["covered"]:
                problems.append(f"{sym}: not covered ({c['why']})")

    storage = storage_check(con)
    if not storage["ok"]:
        problems.append("sqlite: " + "; ".join(storage["problems"]) + " (candle verdict is blind to this)")

    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "db": str(db_path or SQLITE_PATH),
        "ok": candles_ok,
        "ok_all": candles_ok and storage["ok"] and not any(
            not c["covered"] for c in (cov or {}).values()),
        "coverage": {s: {"covered": c["covered"], "why": c["why"],
                         "bars_win": c["bars_win"], "lag_h": c["cover_lag_h"]}
                     for s, c in (cov or {}).items()},
        "storage": storage,
        "problems": problems,
        "detail": detail,
        "symbols": SYMBOLS,
        "timeframes": TIMEFRAMES,
    }

    if publish:
        out = REPORTS_DIR / "data-health.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    con.close()
    return report


def main() -> int:
    report = check_data_health(publish=True)
    print(json.dumps({k: report[k] for k in
                      ("checked_at_utc", "db", "ok", "ok_all", "coverage",
                       "storage", "problems")},
                     indent=2))
    # Exit on ok_all (candles + storage + coverage), not on the candle-only
    # verdict: the coverage gate exists so that a store which is fresh only
    # under the symbols someone remembered to check cannot pass silently.
    return 0 if report["ok_all"] else 1


if __name__ == "__main__":
    raise SystemExit(main())