#!/usr/bin/env python3
"""Ensemble paper book — forward paper record for the strategy-book ensembles.

WHY A SEPARATE BOOK
-------------------
The existing unified-paper.json is the live scanner's forward record. Its
history is what every claim about the system is measured against. If ensemble
signals were written into it, the two would be blended and neither could be
audited — the exact failure the repo's own AGENTS.md forbids ("paper and real
must never be blended", and by extension, two different paper systems must not
be either).

So this writes its OWN book: data/ensemble-paper.json. Same discipline as the
shadow books under /opt/livr-research, which are kept separate for the same
reason.

WHAT IT TRADES
--------------
The ensembles from data/strategy-book/ensembles.json. Each is a GROUP of
independently-gated strategies sharing a (family, timeframe). A member votes for
its own symbol when its rule fires; the ensemble takes a position when enough
members agree.

The two live ensembles are structurally different and the runner respects that:

  momentum / 5m        high win rate (58-61%), rr 1.5, fast
  channel_breakout/4h  low win rate (32-35%), rr 3.0, slow

Each keeps its own signal generation, stop and target geometry. They are not
averaged into one rule — that is the whole point of choosing an ensemble over a
blend.

SIZING AND SAFETY
-----------------
Paper only. No exchange call, no key, no order. Size is a flat notional per
position so P&L is comparable across members. Costs are charged at the measured
0.28% round trip on every close, so the record is net from the first trade —
which is where the real book's problem was hidden.

Read-only on every live state file. Writes only to its own book.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

AGENT = Path("/opt/trading-agent")
DB = AGENT / "data/trading.db"
BOOK = AGENT / "data/ensemble-paper.json"
ENSEMBLES = AGENT / "data/strategy-book/ensembles.json"

COST = 0.0028          # 0.28% round trip, charged on every close
SIZE = 5.0             # flat notional per position, USDT
MAX_OPEN_PER_ENS = 3   # a single ensemble cannot stack unbounded positions

# Geometry is calibrated on the member's OWN bar, not the ensemble's. `rr` alone
# is not enough either: 3.0 = 2% stop x 1.5 and 1.0 = 2% stop x 1.5 at a
# different hold. So the requirement is stated in return space — the target must
# sit inside the move a position of THIS P(hold) is plausibly for. Measured on
# BTC (trading.db, 2026-10-21): P(+3% within 48 bars) is 1.9% on 5m but 11.7% on
# 30m, so a 3% target validates at 30m/48 and never at 5m/48. Reaching it was
# 0/170; the point of this gate is that a target stays reachable-or-labelled.
MIN_P_MARGIN = 0.05    # walk-forward realised frequency of hitting an upside level
CEILING_STEP = 0.5     # % resolution of the swept upside levels
CEILING_GRID_PCTS = (3.0, 6.0, 9.0, 12.0)   # every target the ensembles size (2% * rr)
FULL_HISTORY_DAYS = 365     # "is this ceiling a property of the market or of the month?"
RECENT_DAYS = 30
RECENT_MIN_WINDOWS = 100    # below this the recent window is not a measurement
FALLBACK_PCT = 3.0          # ceiling side of a refused target — the narrowest
                            # geometry any live ensemble sizes; a refusal means
                            # "narrower than this", never "unreachable, give up"

# A REFUSAL IS NOT A STATE, IT IS AN ATTEMPT (2026-11-10, T0.19)
# ---------------------------------------------------------------
# The gate below refuses a target the hold window cannot reach. Correct — but
# the book STOPPED there. Every ensemble sizes its target as 2% * rr, and no
# ensemble in the strategy book carries `rr` as a knob, so a refused geometry
# is refused forever: the door is shut and no run can reopen it.
#
# Measured from logs/ensemble-paper.log (10,766 runs, 2026-10-04 -> 11-10):
#   * 5,181 GEOMETRY REFUSED lines since 2026-10-22T01:05 (momentum|5m;
#     momentum|30m from 15:15);
#   * last open 2026-11-05T05:00:53, then 1,538 consecutive runs = 6.4 days
#     with opened=0 (1,336 of those runs refused);
#   * whole book: 205 closed, -8.34 net after 0.28%, 18.5% WR, TP 0 / SL 57 /
#     TIME 148;
#   * re-measured today on BTC in trading.db: 5m P(reach +3%/48 bars) 0.4-1.05%
#     (window-dependent), 30m P(reach +3%/12 bars) 1.71% — both far under the
#     5% floor, both windows (own and one-year) say no. The earlier "5m 1.4%"
#     did not reproduce; the verdict is unchanged but the figure swings ~2.6x
#     with window length, so quote a range, not a point.
# The wrapper prints nothing when a run opens and closes nothing, so a dead
# lane and a quiet one look identical. The card therefore states the streak,
# the last open, and that the cause is geometry *unless the writer changed*.
#
# This file changes REPORTING ONLY. It does not lower MIN_P_MARGIN, does not
# pick a narrower target, and does not reopen the lane: both are owner calls
# (E15). `rr` is a strategy-book parameter, and re-sizing to a reachable target
# is a different strategy (docs/proposals/ensemble-geometry-measured-2026-10-17).
DEAD_LANE_RUNS = 24         # ~2 hours of "opened 0" is a state, not a quiet spell

TF_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def _load(p: Path, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(d: dict) -> None:
    BOOK.parent.mkdir(parents=True, exist_ok=True)
    tmp = BOOK.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(BOOK)          # atomic: a kill cannot leave a half-written book


def _candles(cur, sym: str, tf: str, limit: int):
    cur.execute(
        "SELECT open_time_utc, open, high, low, close FROM candles "
        "WHERE symbol=? AND timeframe=? ORDER BY open_time_utc DESC LIMIT ?",
        (sym, tf, limit),
    )
    return list(reversed(cur.fetchall()))


def _hold_ms(pos: dict) -> int:
    """How long a position may live, in ms, from ITS OWN recorded bar size.

    `open_ms` is the timestamp of a bar in the ensemble's timeframe, so the bar
    size must come from there. Position timestamps do NOT always land on a
    multiple of their nominal size (measured: a 30m position at 1792587600000 =
    13:00 UTC, a 5m one at 1792584300000 = 12:05) so this never infers tf from
    the ms value. A record without `tf_ms` cannot be aged — None, not a guess.
    """
    try:
        tf_ms = int(pos["tf_ms"])
        hold = int(pos["hold_bars"])
    except (KeyError, TypeError, ValueError):
        return 0
    if tf_ms <= 0 or hold <= 0:
        return 0
    return tf_ms * hold


def _reach_freqs(closes, hold_bars: int, max_pct: float) -> dict:
    """{offset%: P(close within `hold_bars` bars is >= offset% above the entry)}.

    Walk-forward and non-clairvoyant: bar i is judged only on bars i+1..i+hold.
    Offsets are swept to `max_pct` — NOT to a fixed cap, because a ceiling that
    cannot be reached by the sweep can never certify a larger target (the 6%
    channel_breakout target was measured against a 0-3% sweep and came back
    unmeasurable for arithmetic reasons, not market ones).

    Monotone BY CONSTRUCTION: reaching a higher offset implies reaching every
    lower one, so a level is kept only if its frequency is >= the level below.
    The measured frequencies do fall monotonically, but a ceiling that depended
    on that would be a ceiling that could report a far target reachable while
    a nearer one was not.
    """
    n = len(closes)
    if n < 20:
        return {}
    hold_bars = max(1, min(int(hold_bars), n - 2))
    hi_run = [max(closes[i + 1:i + 1 + hold_bars]) if i + 1 < n else closes[i]
              for i in range(n)]
    windows = list(range(n - hold_bars))
    # Level 0 is withheld: the target sits ABOVE the entry, and P(>=0) is not a
    # reachability statement (every flat bar would certify a 0% target).
    levels = [round(x * CEILING_STEP, 1)
              for x in range(1, int(round(max_pct / CEILING_STEP)) + 1)]
    out = {}
    prev = 1.0
    for x in levels:
        need = 1 + x / 100
        reached = sum(1 for i in windows if closes[i] and hi_run[i] / closes[i] >= need)
        # A threshold is only claimable when real evidence reached it. A band hit
        # on 3 windows is not a ceiling; 10 checked windows is the floor.
        if len(windows) < 10 or not reached:
            break
        f = reached / len(windows)
        if f > prev:          # cannot happen if the sweep is sound; refuse it anyway
            f = prev
        out[x] = f
        prev = f
    return out


def margin_pct(rows, hold_bars: int, target_pct: float = FALLBACK_PCT) -> float | None:
    """How far above spot a target can sit before it is unreachable by design?

    The largest offset reached at least MIN_P_MARGIN of the time, swept past
    `target_pct` so that a large target can be certified. None means the sweep
    never reached a claimable level at all (too few windows, or a market that
    never moved MIN_P_MARGIN of the time even +0.5%) — NOT "this target is
    unreachable". The caller must treat those two differently.

    The purpose is to stop the book recording a geometry it never tested. The
    previous instrument held a 3%-scaled target for 12 bars of 30m — six hours —
    while the member was built on 5m bars. That is not a conservative version of
    the member, it is a different strategy, and no verdict about the member
    survives it.
    """
    freqs = _reach_freqs([r[4] for r in rows], hold_bars,
                         max(target_pct, CEILING_GRID_PCTS[0]) + CEILING_STEP)
    claimable = [x for x, f in freqs.items() if f >= MIN_P_MARGIN]
    if not claimable:
        return None
    return round(max(claimable), 1)


def is_unreachable(rows, hold_bars: int, target_pct: float) -> bool:
    """True only when the target is ABOVE a ceiling that was actually measured.

    The old code decided from `margin_pct(...) is None -> open anyway`, which
    read "could not measure" as "fine", and from a ceiling capped below the
    target, which made every target over the cap look certified. Refusal is now
    a comparison against the frequency at the target's OWN level: a target is
    reachable when P(reach it) >= MIN_P_MARGIN, full stop.
    """
    freqs = _reach_freqs([r[4] for r in rows], hold_bars, target_pct)
    if target_pct not in freqs:
        # The sweep stopped before the target: the market hit some NEARER level
        # less than MIN_P_MARGIN of the time first (reaching far implies near).
        # That IS a measured refusal, and the ceiling is the last level reached.
        return True
    return freqs[target_pct] < MIN_P_MARGIN


def ceiling_note(rows, hold_bars: int, target_pct: float) -> dict:
    """The measurement behind an admission, stamped onto the position.

    A ceiling is a property of the WINDOW it was measured on. The 5m book holds
    ~10 days of BTC candles; on that window BTC reached +2% within 48 bars 2.4%
    of the time, while over a year of 5m BNB/XRP bars it reached +4% 4.2-4.7% of
    the time. Neither number is wrong, and a reader who sees only one cannot
    tell which question was answered. So record the short window that admits
    the trade AND whether the long window would have refused it.
    """
    freq = _reach_freqs([r[4] for r in rows], hold_bars, target_pct).get(target_pct)
    note = {"target_pct": round(target_pct, 2),
            "p_reach": None if freq is None else round(100 * freq, 2),
            "measured_n": len(rows),
            "measured_from": _iso_ms(rows[0][0]) if rows else None,
            "measured_to": _iso_ms(rows[-1][0]) if rows else None}
    return note


def _iso_ms(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def long_window_verdict(cur, symbol: str, tf: str, hold_bars: int,
                        target_pct: float) -> dict:
    """Re-measure the SAME geometry on a year of bars, and say if it disagrees.

    Opens its own cursor so the caller's iteration is untouched: stepping a
    shared cursor inside the ensemble loop silently skips ensembles, which is
    the class of bug that made a runner report on a book it never read.
    """
    tf_ms = TF_MS.get(tf)
    if not tf_ms:
        return {"tested": False, "reason": f"unknown timeframe {tf!r}"}
    want = int(FULL_HISTORY_DAYS * 86_400_000 / tf_ms)
    cut = int(RECENT_DAYS * 86_400_000 / tf_ms)
    old = cur.connection.cursor()
    try:
        old.execute(
            "SELECT open_time_utc, open, high, low, close FROM candles "
            "WHERE symbol=? AND timeframe=? ORDER BY open_time_utc DESC LIMIT ?",
            (symbol, tf, want))
        rows = list(reversed(old.fetchall()))
    except sqlite3.Error as e:                      # never kill the runner
        return {"tested": False, "reason": str(e)[:80]}
    finally:
        old.close()
    if len(rows) <= cut + 20:
        return {"tested": False,
                "reason": f"only {len(rows)} bars of history (need > {cut + 20})"}
    long_f = _reach_freqs([r[4] for r in rows], hold_bars, target_pct).get(target_pct)
    recent = rows[cut:] if cut * 3 < len(rows) else rows
    recent_f = _reach_freqs([r[4] for r in recent], hold_bars, target_pct).get(target_pct)
    long_ok = long_f is not None and long_f >= MIN_P_MARGIN
    return {"tested": True,
            "bars": len(rows),
            "from": _iso_ms(rows[0][0]), "to": _iso_ms(rows[-1][0]),
            "long_p": None if long_f is None else round(100 * long_f, 2),
            "recent_bars": len(recent),
            "recent_p": None if recent_f is None else round(100 * recent_f, 2),
            "long_confirms": long_ok,
            "disagrees": recent_f is not None and long_ok != (recent_f >= MIN_P_MARGIN)}



def _rule_fires(rows, lb: int, family: str) -> bool:
    """Does this member's rule fire on the latest closed bar?"""
    if len(rows) < lb + 2:
        return False
    last = rows[-1]
    prior = rows[-lb - 1:-1]
    if not prior:
        return False
    if family == "momentum":
        sma = sum(r[4] for r in prior) / len(prior)
        return last[4] > sma
    if family == "channel_breakout":
        hi = max(r[2] for r in prior)
        return last[4] > hi
    return False


def _fresh_refusals(refused: list, since: dict | None) -> list:
    """The refusals in THIS run that the previous run did not already print.

    The runner's output is appended to a log that is never rotated. Idle runs
    are silent by design (`run_ensemble_paper.sh.orig`: "Reports ONLY when
    something opened or closed"), so the refusal block was the only thing a
    dead lane ever printed — 5,181 times, one per run, from the same three
    lines. Repeating an unchanged verdict every 5 minutes is how a real alarm
    stops being read. A refusal that CHANGES (target, ceiling, scope, or a new
    ensemble) still prints at once, and the dead-lane block below carries the
    state, so dropping the repeat does not hide anything.
    """
    if not since:
        return list(refused or [])
    out = []
    for eid, tgt, ceil, scope in refused or []:
        key = str(eid)
        if str(since.get(key)) == f"{tgt}|{ceil}|{scope}":
            continue
        out.append((eid, tgt, ceil, scope))
    return out


def _stamp_refusals(st: dict, refused: list) -> None:
    """Remember what this run refused, so the next run can tell repeat from news."""
    st["meta"]["refusals"] = {str(eid): f"{tgt}|{ceil}|{scope}"
                              for eid, tgt, ceil, scope in refused or []}


def _lane_silence(st: dict, ens_doc: dict, now=None, fired=None,
                  refused_ids=None) -> dict:
    """How long the book has gone without opening, and WHY if the reason is known.

    A quiet lane and a dead lane were indistinguishable: `run_ensemble_paper.sh`
    prints nothing when a run opens and closes nothing, and a runner that
    REFUSES every candidate every run opens and closes nothing. Measured
    2026-11-10: 1,538 runs / 6.4 days with opened=0, 1,336 of them refusing a
    target the hold window cannot reach — reported nowhere.

    Attribution is by ELIMINATION, and each branch says which case it is:
      * `rr` differs from what this book was built on -> the geometry moved,
        so a dead lane is the writer's change, not the refusal;
      * an ensemble that FIRED this run was refused -> geometry is the cause
        ("unless the writer changed", because a silent rule change looks the
        same from here). `fired` comes from the scan; it is NOT the refusal
        list, which holds stale entries from runs whose rule never fired;
      * nothing fired -> quiet, not dead, and no reason is invented.
    """
    now = now or datetime.now(timezone.utc)
    meta = st.get("meta") or {}
    streak = int(meta.get("silent_runs") or 0)
    if streak < DEAD_LANE_RUNS:
        return {"dead": False, "runs": streak}
    try:
        last_open = datetime.fromisoformat(str(meta.get("last_open_utc")))
        if last_open.tzinfo is None:
            last_open = last_open.replace(tzinfo=timezone.utc)
        days = (now - last_open).total_seconds() / 86400.0
        last_open_s = f"{last_open.isoformat()[:19]}Z ({days:.1f}d)"
    except (TypeError, ValueError):
        last_open_s = "never recorded"
    rrs_now = {str(e.get("id")): _member_rr(e) for e in (ens_doc.get("ensembles") or [])}
    rrs_then = meta.get("rr_by_ensemble") or {}
    changed = sorted(k for k, v in rrs_now.items() if k in rrs_then and v != rrs_then[k])
    if changed:
        cause = (f"GEOMETRY CHANGED since the last open ({', '.join(changed)}): the "
                 f"lane is dead for a different reason — re-check before acting")
    elif refused_ids:
        cause = (f"REFUSED on geometry ({', '.join(sorted(refused_ids))}) — unless the "
                 f"writer changed; the target a reachable hold allows is an owner call")
    else:
        cause = "no candidate has fired; the lane is quiet, not proven dead"
    return {"dead": True, "runs": streak, "last_open": last_open_s, "cause": cause}


def _last_book_time(closed: list) -> str | None:
    """The newest timestamp the book actually carries, as an ISO string.

    Used ONLY to seed `last_open_utc` on a book that predates the field. A close
    is always at or after its open, so the newest close is a CONSERVATIVE
    (late) proxy for the newest open — it can understate the silence, never
    overstate it. Prefers `open_ms` (epoch-ms, the field every record has) and
    falls back to the ISO `ts_utc`. Returns None for an empty book rather than
    a clock reading: "no evidence yet" is a different state from "opened now".
    """
    best = None
    for t in closed or []:
        v = t.get("open_ms")
        if isinstance(v, (int, float)) and v > 1e11:
            best = v if best is None else max(best, v)
    if best is not None:
        return datetime.fromtimestamp(best / 1000.0, timezone.utc).isoformat()
    ts = [str(t.get("ts_utc")) for t in closed or [] if t.get("ts_utc")]
    return max(ts) if ts else None


def _member_rr(ens: dict) -> float | None:
    """The member's `rr` as the runner reads it (last field of member id 0)."""
    try:
        return float((ens.get("member_ids") or [""])[0].split("|")[5])
    except (IndexError, TypeError, ValueError):
        return None


def main() -> int:
    ens_doc = _load(ENSEMBLES, {}) or {}
    ensembles = ens_doc.get("ensembles") or []
    if not ensembles:
        print("no ensembles in the strategy book — nothing to paper")
        return 0

    st = _load(BOOK, {}) or {}
    st.setdefault("open", [])
    st.setdefault("closed", [])
    st.setdefault("meta", {"created_utc": datetime.now(timezone.utc).isoformat(),
                           "cost_per_round_trip": COST, "size_usdt": SIZE})
    # HOW LONG HAS THE BOOK BEEN SILENT? (2026-11-10, T0.19) Read BEFORE this
    # run mutates anything, so `st["meta"]` still describes the run before it.
    _meta_before = json.loads(json.dumps(st["meta"]))
    _prev_open = len(st["open"])

    if not DB.exists():
        print("no candle DB")
        return 1
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.cursor()

    opened = closed = 0
    unaged = []          # open records with no tf_ms — cannot be timed out
    short = []           # open records whose history stops before their horizon ends
    refused = []         # (ensemble, target%, ceiling%, scope) geometry rejected at entry
    fired = set()        # ensembles whose rule FIRED this run
    refused_ids = set()  # ensembles that fired AND were refused on geometry

    for ens in ensembles:
        fam = ens["family"]
        tf = ens["timeframe"]
        symbol = None
        # the ensemble's members carry (symbol, tf, lb, hold, rr); use the FIRST
        # member's symbol as the traded instrument, and the group median params
        mids = ens.get("member_ids") or []
        if not mids:
            continue
        # member id format: family|symbol|tf|lb|hold|rr
        try:
            parts = mids[0].split("|")
            symbol = parts[1]
            lb = int(parts[3])
            hold = int(parts[4])
            rr = float(parts[5])
        except Exception:
            continue

        # The TARGET is sized on THIS timeframe; the HOLD is not. That is fine —
        # a runner may read 30m bars to evolve a position sized for a 5m member.
        # Holding 12 bars of 30m for a 5m-scaled target is not (see hold_ms and
        # the geometry check at entry): it inflated the horizon and made the
        # 5m lane's history a 15-hour-hold backtest. The open block therefore
        # uses the member's OWN hold_bars for its geometry and the ensemble's
        # for its life; margin_pct returns None when that pair is not sane.

        # ── manage open positions for this ensemble ──────────────────────────
        keep = []
        for pos in st["open"]:
            if pos.get("ensemble") != ens["id"]:
                keep.append(pos)
                continue
            hold_ms = _hold_ms(pos)
            if not hold_ms:
                # Record written before tf_ms existed. The bar size comes from
                # the record's OWN `timeframe` field. It must NOT fall back to
                # the current ensemble's tf: an untagged record would then be
                # closed at a horizon nobody recorded for it (caught by
                # test_main_does_not_age_a_record_it_cannot_size).
                tf_ms = TF_MS.get(pos.get("timeframe") or "")
                if tf_ms and int(pos.get("hold_bars") or 0) > 0:
                    pos["tf_ms"] = tf_ms
                    pos.setdefault("tf_ms_backfilled", "from record timeframe")
                    hold_ms = _hold_ms(pos)
            # The bar at which the horizon ENDS, not the newest bar. A 48-bar 5m
            # hold on a position left open for 105 bars is judged on bars
            # 1..48; fetching the last 200 candles and taking rows[-1] priced it
            # at bar 105 (+0.42% worse here) and only looked right because 200
            # bars happened to cover the window. Wide enough to SLOW the request
            # if the deadline is far away, exact once it is inside reach.
            tf_ms = TF_MS.get(pos.get("timeframe") or tf) or TF_MS.get(tf)
            latest = _candles(cur, pos["symbol"], tf, 200)
            if not latest:
                keep.append(pos)
                continue
            due = latest[-1]
            if hold_ms and tf_ms:
                deadline = pos["open_ms"] + hold_ms
                if latest[-1][0] >= deadline:
                    inside = [r for r in latest if r[0] <= deadline]
                    due = inside[-1] if inside else latest[0]
                # Reach BACK TO THE OPEN, not "as far as the deadline is from
                # now": a position 300 bars old has a negative need, and
                # max(negative, 200) never widens — it re-fetched the same 200
                # bars forever. `elapsed` counts from the open bar, so the fetch
                # must also span the open itself: a 300-bar-old position on a
                # series with 150 bars of prior history needed 150+300+48+24.
                elapsed = int((latest[-1][0] - pos["open_ms"]) / tf_ms)
                need = elapsed * 2 + int(pos.get("hold_bars") or 0) + 24
                if need > len(latest):
                    wider = _candles(cur, pos["symbol"], tf, need)
                    inside = [r for r in wider if r[0] <= deadline]
                    if inside:
                        due = inside[-1]
            # `due` must be the bar that ENDS the hold, not one after it. If the
            # stored history stops before bar 0..hold ends, closing on a later
            # price is the defect this whole path exists to stop — leave it for
            # the next run and say why.
            if hold_ms and due[0] > pos["open_ms"] + hold_ms:
                short.append(pos.get("ensemble"))
                keep.append(pos)
                continue
            exit_px = None
            reason = None
            # the stop/target are checked on the bar that closed the horizon,
            # so an exit is never credited to a bar after the position was due
            if due[3] <= pos["sl"]:              # low <= stop
                exit_px, reason = pos["sl"], "SL"
            elif due[2] >= pos["tp"]:            # high >= target
                exit_px, reason = pos["tp"], "TP"
            elif hold_ms and due[0] - pos["open_ms"] >= hold_ms:
                # time stop: exit at the close of the bar that ends the horizon
                exit_px, reason = due[4], "TIME"
            elif not hold_ms:
                unaged.append(pos.get("ensemble"))
            if exit_px is None:
                keep.append(pos)
                continue
            move = (exit_px - pos["entry"]) / pos["entry"]
            pnl = SIZE * (move - COST)
            stamp = {
                "exit": round(exit_px, 6), "pnl": round(pnl, 6),
                "reason": reason,
                # WHEN the money moved (the priced bar) is not WHEN it was
                # noticed (the scan). Stamping only the scan time made a horizon
                # exit look like it happened at 01:05 on a bar that closed hours
                # earlier, and left no way to audit which bar was priced.
                "exit_bar_ms": due[0],
                "exit_bar_utc": _iso_ms(due[0]),
                "bars_held": round((due[0] - pos["open_ms"])
                                   / (pos.get("tf_ms") or 1), 2),
                "close_ts_utc": datetime.now(timezone.utc).isoformat(),
            }
            pos.update(stamp)
            st["closed"].append(pos)
            closed += 1
        st["open"] = keep

        # ── open a new position if the rule fires ────────────────────────────
        n_open = sum(1 for p in st["open"] if p.get("ensemble") == ens["id"])
        if n_open >= MAX_OPEN_PER_ENS:
            continue
        # dedupe: same ensemble+symbol already open
        if any(p.get("ensemble") == ens["id"] and p.get("symbol") == symbol
               for p in st["open"]):
            continue

        rows = _candles(cur, symbol, tf, lb + 5)
        if not _rule_fires(rows, lb, fam):
            continue
        fired.add(str(ens.get("id")))
        last = rows[-1]
        entry = last[4]
        # geometry follows the member's own rr, at a 2% stop base, matching the
        # discipline the existing paper book uses (stop calibrated, rr derived)
        stop_pct = 0.02
        target_pct = round(stop_pct * rr * 100, 2)
        # REFUSE a geometry the bar size cannot produce. Before 2026-10-21 this
        # ran unconditionally, so ens|momentum|5m held a +3% target for 48 * 5m*
        # bars — a horizon on which BTC exceeded +3% only 1.9% of the time. The
        # book then reported 0 TP in 170 closes and that number was read as a
        # statement about the momentum rule.
        #
        # The horizon is `hold` — the number of bars THIS position is aged on
        # (manage uses tf_ms * hold_bars). Measuring the ceiling over
        # max(hold, lb) certifies a horizon the book never holds: on real BTC 5m
        # the 96-bar lookback gave a 2.1% ceiling for a 48-bar hold (1.6%),
        # admitting exactly the unreachable targets this gate exists to stop.
        scope = "own bars"
        margin_rows = rows
        if len(rows) < hold + 20:
            # Too few bars to answer over the live position's life: fetch a
            # real window. Refusing here would report "unreachable" when the
            # truth is "not yet measured" — a different finding.
            margin_rows = _candles(cur, symbol, tf, hold * 3 + 60)
            scope = "widened window"
        ceiling = margin_pct(margin_rows, hold, target_pct)
        unreachable = is_unreachable(margin_rows, hold, target_pct)
        if unreachable:
            # A refusal is a statement about the LAST year, not just today's
            # candles. Re-measure the same geometry on a year of bars before
            # standing the ensemble down, so a quiet fortnight cannot silently
            # freeze the book (see long_window_verdict).
            longv = long_window_verdict(cur, symbol, tf, hold, target_pct)
            if longv.get("tested") and longv.get("long_confirms"):
                # Narrow window says no, the year says yes: admit it, and label
                # the disagreement rather than pretending it does not exist.
                unreachable = False
                scope = f"admitted on year-window (recent p={longv.get('recent_p')}% < {MIN_P_MARGIN*100:.0f}%)"
        if unreachable:
            refused.append((ens["id"], target_pct, ceiling, scope))
            refused_ids.add(str(ens.get("id")))
            continue
        note = ceiling_note(margin_rows, hold, target_pct)
        tp = round(entry * (1 + stop_pct * rr), 6)
        sl = round(entry * (1 - stop_pct), 6)
        st["open"].append({
            "ensemble": ens["id"], "family": fam, "timeframe": tf,
            "symbol": symbol, "entry": round(entry, 6),
            "tp": tp, "sl": sl, "rr": rr, "hold_bars": hold,
            # bar size of the bars this position is aged on. Persisted because
            # the manage loop cannot re-derive it from a timestamp (see _hold_ms).
            "tf_ms": TF_MS[tf], "target_pct": target_pct,
            # the reachability measurement that ADMITTED this target, on the
            # window it was measured on — not a bare boolean
            "target_ceiling_pct": ceiling, "reach": note, "reach_scope": scope,
            "size": SIZE, "open_ms": last[0],
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "mode": "paper",
        })
        opened += 1

    con.close()

    # SILENCE ACCOUNTING (2026-11-10, T0.19) — written BEFORE the print block,
    # and from the file's own history: a state file that never records "no open
    # in 6 days" cannot be asked the question afterwards. `rr_by_ensemble` is
    # stamped only when the book opens, so "the writer changed the geometry" is
    # distinguishable from "the same geometry is still refused".
    #
    # THE STREAK COUNTS RUNS THE RUNNER ACTUALLY RAN, not wall-clock slots: a
    # `last_run_utc` stamped by a killed process would make a dead runner look
    # alive, and this file has already been burned by that (the v2 watchdog's
    # `runner_silent` died with the runner it watched, E13). The caller sees
    # `last_run_utc` because it is written HERE, by a run that reached the end.
    _now = datetime.now(timezone.utc)
    st["meta"]["last_run_utc"] = _now.isoformat()
    st["meta"]["opens_total"] = st["meta"].get("opens_total", 0) + opened
    st["meta"]["closes_total"] = st["meta"].get("closes_total", 0) + closed
    st["meta"]["silent_runs"] = 0 if opened else int(_meta_before.get("silent_runs") or 0) + 1
    # A BOOK WITH NO `last_open_utc` HAS NOT "JUST OPENED" — it has never been
    # asked. Seeding it from the clock made the dead-lane card print
    # "NO OPEN IN 24 RUNS (last open <today> (0.1d))" while opens_total was 205
    # and the newest closed row was 2026-11-05: the streak said 24, the age said
    # 0.1d, and the two describe different events. The seed comes from the BOOK
    # (its newest close was necessarily at or after its newest open), and a book
    # with neither a prior stamp nor a close gets no stamp at all — `_lane_silence`
    # then says "never recorded" instead of inventing a time. (2026-11-10, T0.19)
    if opened:
        st["meta"]["last_open_utc"] = _now.isoformat()
        st["meta"]["rr_by_ensemble"] = {str(e.get("id")): _member_rr(e) for e in ensembles}
    elif not st["meta"].get("last_open_utc"):
        seed = _last_book_time(st["closed"])
        if seed:
            st["meta"]["last_open_utc"] = seed
            st["meta"]["rr_by_ensemble"] = {str(e.get("id")): _member_rr(e) for e in ensembles}
    # REPEATS DROPPED (2026-11-10, T0.19): an unchanged refusal is state, not
    # news. It is printed when it first appears or when it CHANGES; the dead-lane
    # block below carries it on every run while the lane stays shut. Otherwise a
    # reader gets the same three lines 288 times a day and stops seeing them.
    fresh = _fresh_refusals(refused, _meta_before.get("refusals"))
    _stamp_refusals(st, refused)
    _lane = _lane_silence(st, ens_doc, now=_now, fired=fired,
                          refused_ids=refused_ids)
    _save(st)          # ONE atomic write, after every field is final

    n_open = len(st["open"])
    n_closed = len(st["closed"])
    print(f"ensemble paper book  {_now.isoformat()[:19]}")
    print(f"  opened this run : {opened}")
    print(f"  closed this run : {closed}")
    print(f"  open now        : {n_open}")
    print(f"  closed total    : {n_closed}")
    if fresh:
        by = {}
        for eid, tgt, ceil, scope in fresh:
            by.setdefault(eid, []).append((tgt, ceil, scope))
        for eid, v in by.items():
            tgt, ceil, scope = v[0]
            # Name the measurement, not a verdict: "ceiling 3.0%" for a 6% target
            # reads as "the market stops at 3", which the sweep never claimed.
            claim = (f"ceiling {ceil}%" if ceil is not None
                     else "no level cleared the floor")
            print(f"  GEOMETRY REFUSED: {eid} target +{tgt}% — P(reach) below "
                  f"{MIN_P_MARGIN:.0%} over its {scope} ({claim}); "
                  f"{len(v)} attempt(s), no position opened")
    if _lane.get("dead"):
        print(f"  NO OPEN IN {_lane['runs']} RUNS (last open {_lane.get('last_open')})")
        print(f"    cause: {_lane['cause']}")
        # A paper book that stopped recording also stopped being evidence. Say
        # it here, because "205 closed" is the number the digest quotes.
        print(f"    the {n_closed} closed rows are the LAST record this book made; "
              f"nothing since has been measured")
    if unaged:
        print(f"  UN-AGEABLE OPEN : {sorted(set(unaged))} carry no tf_ms; they "
              f"cannot time out and are left untouched")
    if short:
        print(f"  SHORT HISTORY   : {sorted(set(short))} have no bars covering "
              f"their hold; left open rather than priced on a later bar")
    if n_closed:
        pnl = sum(p.get("pnl", 0) for p in st["closed"])
        wins = sum(1 for p in st["closed"] if p.get("pnl", 0) > 0)
        print(f"  net P&L (after {COST:.2%} round trip) : {pnl:+.4f}")
        print(f"  win rate        : {wins / n_closed * 100:.1f}%  ({wins}W/{n_closed - wins}L)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
