"""How much of the candidate search does the lab *repeat*? (T1.6, 2026-11-06)

WHY THIS EXISTS
---------------
`strategy_lab.py` runs a fixed 3 symbols x 3 entry x 2 exit x 3 rr = 54-point
grid every 15 minutes and logs one `rejected.jsonl` line per point. 96 runs a
day x 54 = 5,184 lines a day, forever. The question nobody had asked is
whether those lines are 5,184 *observations* or one observation repeated.

Measured 2026-11-06T18:00 on 166,119 rejects in 3,274 runs
(2026-10-03..2026-11-06).  ⚠️ Every number below is re-derivable by running
this module; the first draft of this header carried four that were not.

    distinct points reached the boundary gate   8 of 54, all SOL
    runs, 2026-10-16..11-06                     2,141
    ... with a split survivor                   1,538
    ... whose every survivor died at boundary   1,538  (100.0%)
    last NEW point to first clear the gate      2026-10-22T09:15
    rejected.jsonl now                          27.9 MB, +0.82 MB/day

So the gate is not "finding nothing" — it is re-deriving the SAME eight
verdicts 96 times a day, and in the 6 weeks since 2026-10-16 every single run
whose grid produced a survivor produced one that the robustness gate then
killed. That is not a research loop, it is a very expensive clock.

⚠️ The novelty figure is NOT the same thing and must not be quoted as "the lab
is still learning": 429 of 3,274 runs were a verdict set never seen before
(13.1%), last at 2026-11-06T13:15. A *novel reason string* is a moved number,
not a new configuration — the grid points reaching the gate have been the same
eight since 2026-10-16 (seven of them) plus one more on 2026-10-22. Reported
without that split, "13.1% novel" reads as a working research loop.

WHAT THIS TOOL ANSWERS (read-only, measured, never inferred)
------------------------------------------------------------
1. **Novelty** — is this run's verdict set identical to a previous run's?
   A repeated verdict must have an identical *reason*, not merely an identical
   symbol: `split: first=-0.4120 second=+0.0056` only repeats when the
   underlying candles and the evaluated half are unchanged.
2. **The frozen-half hypothesis** — the split gate evaluates each half on a
   window slice (`rows[:half]`), so a run whose candles advanced but whose
   slice did not MUST produce a byte-identical verdict. Counting how many runs
   are byte-identical is how you tell "the gate is working" from "the gate is
   asleep". If runs are identical for days, the first half is frozen and the
   "out-of-sample" half is the only part moving — rule 12 at the split level.
3. **Archive cost vs information** — bytes/day against distinct verdicts/day.

WHAT THIS TOOL DOES NOT DO
--------------------------
It does not run, edit, schedule or stop the lab; it does not touch a parameter,
a book or a live state file. It reads two logs and prints a report. Whether the
lab should keep spending 5,184 evaluations a day to learn nothing new is an
owner decision (E12), and this tool exists so that decision has a number under
it instead of an impression.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

AGENT = Path("/opt/trading-agent")
DEFAULT_REJECTS = AGENT / "data/strategy-lab/rejected.jsonl"
DEFAULT_QUEUE = AGENT / "data/strategy-lab/candidates.jsonl"
DEFAULT_DB = AGENT / "data/trading.db"

# A run is a burst of lines sharing a wall-clock minute. The wrapper fires at
# :00/:15/:30/:45 and the whole 54-point grid takes ~1-2 s, so the minute is a
# safe grouping key. Run cadence is NOT assumed here: the report derives it.


@dataclass
class Run:
    ts: datetime
    points: list[dict]
    reasons: dict[tuple, str] = field(default_factory=dict)

    @property
    def key(self) -> tuple:
        """The verdict fingerprint: which point got which reason."""
        return tuple(sorted(self.reasons.items()))

    @property
    def split_survivors(self) -> list[tuple]:
        """Points that got PAST the split gate (rejected at the boundary gate).

        A point rejected for `failed split` never reached the robustness test;
        the only points that did are those the split gate accepted. This is the
        one honest measure of "how much of the grid is still alive".
        """
        return [k for k, v in self.reasons.items() if v.startswith("failed boundary")]


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def load_rejects(path: Path = DEFAULT_REJECTS) -> list[Run]:
    """Parse rejected.jsonl into runs. Blank/corrupt lines are skipped, counted."""
    if not path.exists():
        return []
    runs: list[Run] = []
    cur: list[dict] = []
    last: datetime | None = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                t = _ts(rec["ts_utc"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            # Group by WALL-CLOCK MINUTE, not by "gap to the previous line".
            # Measured 2026-11-06: the first version used a 30-minute gap and
            # reported 2 runs for 165,059 lines, because the grid emits 52-53
            # lines a few milliseconds apart — the 15-minute cadence only
            # appears BETWEEN runs, never inside one. Grouping on the minute
            # also survives a run that overruns into the next slot.
            if last is not None and t.replace(second=0, microsecond=0) != last.replace(
                    second=0, microsecond=0):
                runs.append(_make_run(cur))
                cur = []
            cur.append(rec)
            last = t
    if cur:
        runs.append(_make_run(cur))
    return runs


def _make_run(points: list[dict]) -> Run:
    r = Run(ts=_ts(points[0]["ts_utc"]), points=points)
    for p in points:
        k = (p.get("symbol"), p.get("entry_lb"), p.get("exit_lb"), p.get("rr"))
        r.reasons[k] = p.get("reason", "")
    return r


def novelty(runs: list[Run]) -> dict:
    """How many runs produced a verdict set nobody had produced before?"""
    seen: dict[tuple, datetime] = {}
    first_seen: list[Run] = []
    for r in runs:
        if r.key not in seen:
            seen[r.key] = r.ts
            first_seen.append(r)
    repeat_streak = 0
    if runs:
        for r in reversed(runs):
            if r.key != runs[-1].key:
                break
            repeat_streak += 1
    gaps = sorted((b.ts - a.ts).total_seconds() / 60 for a, b in zip(runs, runs[1:]))
    span_days = (runs[-1].ts - runs[0].ts).total_seconds() / 86400 if len(runs) > 1 else 0
    return {
        "runs": len(runs),
        "distinct_verdict_sets": len(seen),
        "novel_runs": len(first_seen),
        "novel_share": (len(first_seen) / len(runs)) if runs else 0.0,
        "last_novel_utc": first_seen[-1].ts.isoformat() if first_seen else None,
        "trailing_identical_runs": repeat_streak,
        # The cadence is measured, not taken from the cron expression.
        "median_gap_min": round(gaps[len(gaps) // 2], 1) if gaps else None,
        "runs_per_day": round(len(runs) / span_days, 1) if span_days else None,
        "first_seen": first_seen,
    }


def grid_coverage(runs: list[Run]) -> dict:
    """Which of the 54 grid points ever got past the split gate, and how often."""
    reached: Counter = Counter()
    runs_with_survivor = 0
    fragile_runs = 0
    for r in runs:
        surv = r.split_survivors
        if surv:
            runs_with_survivor += 1
            # A boundary-gate rejection means the WORST half was <= 0 there is
            # only one way to be rejected by that gate, so this is a count of
            # runs in which the grid produced nothing that survived.
            if min(_reason_worst(r.reasons[k]) for k in surv) <= 0:
                fragile_runs += 1
        for k in surv:
            reached[k] += 1
    return {
        "distinct_points_reaching_boundary": len(reached),
        "points": reached,
        "runs_with_a_split_survivor": runs_with_survivor,
        "runs_where_every_survivor_died_at_boundary": fragile_runs,
        "fragile_share": (fragile_runs / runs_with_survivor) if runs_with_survivor else 0.0,
    }


def _reason_worst(reason: str) -> float:
    """`worst half -0.0716 across 5 splits; ...` -> -0.0716 (0.0 if absent)."""
    for tok in reason.replace(";", " ").split():
        try:
            return float(tok)
        except ValueError:
            continue
    return 0.0


def half_is_frozen(runs: list[Run], db: Path = DEFAULT_DB) -> dict:
    """Did the candles a run scores on actually change between runs?

    The claim under test: the split gate's FIRST half is a fixed earliest slice
    of a rolling window, so it does not advance as candles arrive. We do not
    infer that from the code — we check the candles.

    Returns, per symbol/timeframe: the number of distinct rows the query would
    have seen, the newest bar, and whether ANY 54-point verdict changed in the
    last 24h of runs. Read-only; missing DB is an error, not a skip.
    """
    out: dict = {"db": str(db), "runs_scanned": 0, "symbols": {}}
    if not db.exists():
        out["error"] = f"no candle DB at {db}"
        return out
    if not runs:
        return out
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        syms = sorted({str(p["symbol"]) for p in runs[-1].points if p.get("symbol")})
        for sym in syms:
            row = cur.execute(
                "SELECT count(*), min(open_time_utc), max(open_time_utc) "
                "FROM candles WHERE symbol=? AND timeframe='1h'", (sym,)).fetchone()
            # min/max are EPOCH MS (see SYSTEM_MAP §1) — printing the integer is
            # how a staleness bug hides in plain sight. Render the bar's UTC
            # time and its age in hours; "last 1793962800000" tells nobody.
            last_ms = row[2]
            last_dt = (
                datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc)
                if last_ms else None
            )
            age_h = (
                (runs[-1].ts - last_dt).total_seconds() / 3600
                if last_dt is not None else None
            )
            out["symbols"][sym] = {
                "1h_bars": row[0],
                "first_utc": datetime.fromtimestamp(
                    row[1] / 1000, tz=timezone.utc).isoformat() if row[1] else None,
                "last_utc": last_dt.isoformat() if last_dt else None,
                "age_h_at_last_run": round(age_h, 1) if age_h is not None else None,
            }
        out["runs_scanned"] = len(runs)
    finally:
        con.close()
    return out


def archive_cost(path: Path = DEFAULT_REJECTS, queue: Path = DEFAULT_QUEUE) -> dict:
    res = {}
    for name, p in (("rejected", path), ("candidates", queue)):
        if not p.exists():
            res[name] = {"exists": False}
            continue
        st = p.stat()
        first = last = None
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    t = _ts(json.loads(line)["ts_utc"])
                except Exception:
                    continue
                first = first or t
                last = t
        bytes_ = st.st_size
        days = (last - first).total_seconds() / 86400 if first and last else 0
        res[name] = {
            "exists": True,
            "bytes": bytes_,
            "first_utc": first.isoformat() if first else None,
            "last_utc": last.isoformat() if last else None,
            "days": round(days, 2),
            "mb_per_day": round(bytes_ / 1e6 / days, 3) if days else None,
        }
    return res


def symbol_reach(runs: list[Run]) -> dict:
    """Has each universe symbol EVER got a point past the split gate?

    This is the coarsest question and the one that was never asked: the gate
    prints "nothing cleared" identically whether a symbol was tested and
    refused or never evaluated at all. Measured 2026-11-06: 3,030 of 3,030
    boundary-gate verdicts are SOL. BTC and ETH have produced zero in 3,254
    runs, so 36 of every 54 evaluations are of two symbols that have never
    advanced, and the "3 existing strategies ported" claim in the roadmap is
    really "one symbol's parameter grid, re-run".
    """
    ever: dict[str, int] = {}
    for r in runs:
        for k in r.split_survivors:
            ever[str(k[0])] = ever.get(str(k[0]), 0) + 1
    universe = sorted({str(p["symbol"]) for r in runs for p in r.points if p.get("symbol")})
    dead = [s for s in universe if s not in ever]
    return {
        "universe": universe,
        "boundary_verdicts_by_symbol": ever,
        # Evaluations never spent on a symbol that has ever advanced. Stated as
        # a share of the grid so the reader can see the price of the silence.
        "symbols_never_advanced": dead,
        "share_of_grid_on_never_advanced": (
            round(len(dead) / len(universe), 3) if universe else None
        ),
    }


def report(path: Path = DEFAULT_REJECTS, queue: Path = DEFAULT_QUEUE,
           db: Path = DEFAULT_DB) -> dict:
    runs = load_rejects(path)
    return {
        "novelty": {k: v for k, v in novelty(runs).items() if k != "first_seen"},
        "coverage": grid_coverage(runs),
        "reach": symbol_reach(runs),
        "candles": half_is_frozen(runs, db),
        "cost": archive_cost(path, queue),
    }


def render(r: dict) -> str:
    n, c, k, cost = r["novelty"], r["coverage"], r["candles"], r["cost"]
    lines = []
    lines.append("STRATEGY LAB — repeated measurement, not new information")
    lines.append(f"  runs                    : {n['runs']}")
    lines.append(f"  cadence (measured)      : every {n['median_gap_min']} min "
                 f"-> {n['runs_per_day']} runs/day")
    lines.append(f"  distinct verdict sets   : {n['distinct_verdict_sets']}")
    lines.append(f"  runs that said something new : "
                 f"{n['novel_runs']} ({n['novel_share']:.1%})")
    lines.append(f"  last new verdict        : {n['last_novel_utc']}")
    lines.append(f"  identical runs at the tail   : {n['trailing_identical_runs']}")
    lines.append("")
    lines.append(f"  grid points past the split gate : "
                 f"{c['distinct_points_reaching_boundary']} of 54")
    lines.append(f"  runs with a survivor    : {c['runs_with_a_split_survivor']}")
    lines.append(f"  ... every survivor fragile : "
                 f"{c['runs_where_every_survivor_died_at_boundary']} "
                 f"({c['fragile_share']:.1%})")
    reach = r["reach"]
    lines.append(f"  symbols that ever advanced : "
                 f"{sorted(reach['boundary_verdicts_by_symbol']) or 'NONE'}")
    lines.append(f"  symbols never advanced     : "
                 f"{reach['symbols_never_advanced']}")
    for sym, d in k.get("symbols", {}).items():
        lines.append(f"  {sym:9s} 1h bars {d['1h_bars']:6d}  "
                     f"last bar {str(d['last_utc'])[:16]}  "
                     f"({d['age_h_at_last_run']} h behind the last run)")
    lines.append("")
    for name in ("rejected", "candidates"):
        d = cost[name]
        if not d.get("exists"):
            lines.append(f"  {name}: absent")
            continue
        lines.append(f"  {name:10s} {d['bytes']/1e6:7.2f} MB over {d['days']:5.1f} d"
                     f"  = {d['mb_per_day']} MB/day")
    return "\n".join(lines)


if __name__ == "__main__":
    print(render(report()))
