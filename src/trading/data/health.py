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
        # newest 5m OPEN per symbol — the only clock that can see a dead 5m
        # feed while the other timeframes keep landing (max-close cannot).
        last5 = {s: mx for s, mx in con.execute(
            "SELECT symbol, MAX(open_time_utc) FROM candles "
            "WHERE timeframe='5m' GROUP BY symbol")}
        out = {}
        for sym, close in latest_any.items():
            lag_h = (store_latest - close) / 3600e3
            lag5_h = (grid_end - last5[sym]) / 3600e3 if last5.get(sym) else float("inf")
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
            elif lag_h <= grace_h and not holes:
                # Low-frequency symbol refreshed with the store. `not holes` is
                # load-bearing: `lag_h` is a MAX over ALL timeframes, so a dead
                # 5m feed whose 1h bars still land has lag_h 0.0 and used to
                # take this exemption with 32 slots missing — `covered: true`,
                # why '' (measured 2026-10-26 01:08Z, grace defaults to 0.0 so
                # the exemption was landing on symbols with ZERO lag). A hole
                # is the one thing max-close cannot excuse.
                why = ""      # low-frequency symbol, refreshed with the store
            elif bars_win == 0 and not holes:
                why = f"0 bars in {window_hours}h, {lag_h:.1f}h behind the store"
            elif bars_win == 0:
                # NOTE: `lag_h` is NOT evidence of freshness. A dead 5m feed
                # whose 1d bars still land is only 1h "behind the store" by
                # max-close (measured: 0.9h for a 21h-dead 5m series). Only
                # bars inside the window count; the lag is reported beside.
                #
                # The reason must LEAD with what is measurably wrong. This
                # branch used to return `0 bars in 1h` for a series that HAS
                # bars in the hour but is missing a slot inside it — measured
                # on a fixture at 01:04Z: bars_win 0/12 because the newest two
                # 5m slots were 6h old, holes 32, and the line said \"0 bars in
                # 1h\" while 32 slots were the finding. `check_data_health`
                # then suppressed the hole as \"filled\" because the 1h re-read
                # saw no hole in the last hour, and the whole report went
                # green (`ok_all: True, problems: []`) on a 6h-dead 5m feed.
                # A hole and an empty window are different faults and the
                # text is what downstream reads: name the dark streak first.
                why = (f"{len(holes)} holes in {hole_window_h}h, newest 5m bar "
                       f"{lag5_h:.1f}h old ({bars_win}/{window_hours * 12} slots "
                       f"in the last {window_hours}h), {lag_h:.1f}h behind the store")
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


def series_episodes(db_path: Path | None = None, min_minutes: int = 0) -> dict:
    """Staleness as EPISODES with a start, not as a per-run boolean.

    `series_coverage` answers "is this series covered now". That is the wrong
    question to ask twice, because the answer is rewritten every run: an
    outage that started at 04:00 and is still running at 13:00 leaves one
    `covered: false` in `data-health.json` and no trace that it is nine hours
    old. Measured 2026-10-25: seven alts went dark and three monitors gave
    three answers (harness OK, coverage red, ingest "all series fresh"); the
    single fact that would have settled it — when did they go dark, and is
    that the same outage as yesterday's — was in none of them.

    An episode is one contiguous stretch in which a set of symbols has no
    closed 5m bar. Episodes are grouped by `since` to the nearest 5-minute
    grid: symbols that died on the same ingest tick are ONE outage, not seven
    coincidences. `end` is the newest 5m open actually present for that group.

    Returns:
      episodes          [{since_utc, end_utc|None, symbols[], duration_h,
                          ongoing, timeframes[]}]
      open_symbols      how many series have no bar inside the last hour
      open_hours_max    age of the OLDEST still-open episode (0 if none)
      current_holes     symbols uncovered by `series_coverage` right now

    `min_minutes` drops episodes shorter than that (a single 15-minute ingest
    slip is noise; an outage is not).
    """
    path = db_path or SQLITE_PATH
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        now_ms = int(time.time() * 1000)
        grid_end = (now_ms // IV_5M) * IV_5M - IV_5M   # newest closed slot
        last5 = {s: mx for s, mx in con.execute(
            "SELECT symbol, MAX(open_time_utc) FROM candles "
            "WHERE timeframe='5m' GROUP BY symbol")}
        tfs_of: dict[str, set] = {}
        for sym, tf in con.execute("SELECT DISTINCT symbol, timeframe FROM candles"):
            tfs_of.setdefault(sym, set()).add(tf)
    finally:
        con.close()

    # A series is DARK once its newest closed 5m bar is more than one slot
    # plus the ingest slack behind the grid (the same slack coverage uses).
    dark_cut = grid_end - INGEST_SLACK_MS
    groups: dict[int, dict] = {}
    open_symbols = 0
    for sym, mx in last5.items():
        if mx is None or mx >= dark_cut:
            continue
        open_symbols += 1
        since = mx + IV_5M                       # first expected-but-absent slot
        if (now_ms - since) / 60_000 < min_minutes:
            continue
        key = since // (5 * 60_000) * (5 * 60_000)   # same ingest tick → one outage
        g = groups.setdefault(key, {"since": since, "end": mx, "symbols": set()})
        g["symbols"].add(sym)
        g["end"] = max(g["end"], mx)

    episodes = []
    for key in sorted(groups):
        g = groups[key]
        # `end` is the newest bar the group HAS; the episode began at the slot
        # after it. Report the last bar as `last_bar_utc` and the first missing
        # slot as `since_utc`, because "since 03:30" means no bar from 03:30 on.
        since = g["end"] + IV_5M
        dur = (now_ms - since) / 3600e3
        tfs = sorted(set().union(*(tfs_of.get(s, set()) for s in g["symbols"])))
        episodes.append({
            "since_utc": datetime.fromtimestamp(since / 1000, timezone.utc)
                                  .strftime("%Y-%m-%dT%H:%MZ"),
            "last_bar_utc": datetime.fromtimestamp(g["end"] / 1000, timezone.utc)
                                     .strftime("%Y-%m-%dT%H:%MZ"),
            "end_utc": None,          # open: no 5m bar since `since_utc`
            "symbols": sorted(g["symbols"]),
            "duration_h": round(dur, 1),
            "ongoing": True,
            "timeframes": tfs,
        })
    cover = series_coverage(path)
    return {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "episodes": episodes,
        "open_symbols": open_symbols,
        "open_hours_max": max((e["duration_h"] for e in episodes), default=0),
        "current_holes": sorted(s for s, c in cover.items() if not c["covered"]),
    }


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
    eps = None
    if coverage:
        cov = series_coverage(db_path)
        # A hole that has since been FILLED is not a fault. `series_coverage`
        # is a point-in-time verdict over a 3h window, and the write path now
        # settles a late closed bar over a partial one, so a hole a monitor saw
        # at 18:00 can be gone at 18:05. Measured 2026-10-25 on the live store
        # between two direct SELECTs 30 seconds apart: first
        # `BTC/USDT: 1 holes in 3h (first 15:00Z)`, then `holes: 0` — the bar
        # was there and had been written at 13:02.
        #
        # Re-ask with a 1h window and only report a hole that is still missing
        # THERE. The bar count (`bars_win`) is deliberately not used to decide:
        # 11 of 12 bars in the last hour also returns `covered: true` with
        # `holes: 1`, so a count can hide the hole it is supposed to expose.
        stale_holes = {s for s, c in cov.items()
                       if not c["covered"] and "holes in" in c["why"]}
        if stale_holes:
            recheck = series_coverage(db_path, hole_window_h=1)
            for s in list(stale_holes):
                r = recheck.get(s) or {}
                holes_now = r.get("holes")
                old_holes = cov[s].get("holes") or 0
                if holes_now == 0 and old_holes <= 1:
                    # ONLY a single hole can have been filled. Clearing is a
                    # claim about the bar, so it must be true of that bar — and
                    # a hole that fell out of the 1h window is not evidence it
                    # was filled. Measured 2026-10-26 01:04Z on the fixture
                    # below: the newest two 5m slots sat 6h back, 32 slots were
                    # missing, the 1h re-read naturally held no hole (nothing
                    # was expected there), and `holes_now == 0` cleared the
                    # series — `ok_all` true, `problems: []`, on a feed whose
                    # 5m series was six hours dead. A streak of holes is never
                    # "filled late"; it is a dark feed wearing a hole's name.
                    cov[s] = {**r, "why": "",
                              "note": "3h-window hole is absent from the most recent "
                                      "hour (re-read, not assumed)"}
                elif r.get("why"):
                    cov[s] = {**r, "why": r["why"] + " [confirmed in the 1h re-read]"}
                else:
                    # keep the 3h verdict — the re-read window cannot see this
                    # hole, which is not the same as the hole being gone.
                    cov[s] = {**cov[s], "why": cov[s]["why"]
                              + " [still missing on re-derive; outside the 1h re-read]"}
        for sym, c in cov.items():
            if not c["covered"]:
                problems.append(f"{sym}: not covered ({c['why']})")
        # The per-series lines above say WHAT is uncovered now. Only the
        # episode says since WHEN — and one nine-hour outage must not read the
        # same as seven independent one-hour blips.
        eps = series_episodes(db_path)
        for e in eps["episodes"]:
            problems.append(
                f"episode: {len(e['symbols'])} series dark since {e['since_utc']} "
                f"({e['duration_h']}h) — {', '.join(s.split('/')[0] for s in e['symbols'][:4])}"
                f"{' …' if len(e['symbols']) > 4 else ''}")

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
        "episodes": eps,
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