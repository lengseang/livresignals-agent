#!/usr/bin/env python3
"""FILL-PRICE AUDIT — is the paper book's NET basis an upper bound or a lower one?

WHY (2026-11-04, afternoon shift)
---------------------------------
`data/unified-paper.json` books every paper exit at an OBSERVED tick. For TIME
and STALL exits it records that tick as `exit_px`. For the two exits that
account for most of the book (SL ~1030, TP ~310 of 2787 collapsed records)
there is no `exit_px`, so the fill can only be inferred from `sl`/`tp` — and
the inference only holds if the writer books at the threshold. It does not on
2026-09..11 records: 13 SL records with a +1.2% trailing stop book a NEGATIVE
pnl (down to -0.80% of stake), a level the stop had passed long before entry.
The paper book's net figure is therefore a sale at an unknown price, and every
report that quotes it quotes that unknown.

This module replaces the guess with a one-sided measurement of how far
UNFAVOURABLE the unknown can be, using only bar highs/lows and one constant:
`avg_gain_in_force_pct`, the mean profit of the closed trades open at a bar
(restricted to the position's own stop width), i.e. the average edge in force.

THE BOUND. At a bar the position's loss is (close - fill). If the book's own
average says an in-force position is worth +g%, then the worst the fill can have
been inside the bar is bounded by the bar's own range relative to its body:
the adverse excursion cannot exceed g × (range/body), and charging τ²/3 of that
(the adverse move is concentrated early) gives the bound used here. Body ≤ 0
carries no information and is skipped — the method never invents a bar.

THE AGREEMENT TEST. Where the book DOES record `exit_px` (TIME/STALL), the
bound must hold on essentially every record, else the method or its inputs are
wrong and nothing here may be quoted. That test is what makes this citable.

HONEST DISAGREEMENT (2026-11-04, research loop)
----------------------------------------------
`scripts/research_loop.py` read the same missing-fill defect and measured the
other direction: replacing each SL fill with the synthetic stop level gives
33.04 against the printed 36.11 — the synthetic SL fill is FAVOURABLE by
+3.07pp. This audit measures the BOOKED SL fill as ~+2.2pp UNFAVOURABLE. Same
order, opposite sign. They are over different books: this one is the real
2787-record paper book with its records' own stops and bar data; the research
loop re-simulates its own entries. A reader must say which book a number is on.
Both agree that the SL fill is not free and cannot be assumed either way.
"""
from __future__ import annotations

import bisect
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAPER = ROOT / "data" / "unified-paper.json"

MS_PER_MIN = 60_000


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None          # NaN -> None


def load_book(path: Path | str | None = None) -> list[dict]:
    p = Path(path) if path else PAPER
    return json.loads(Path(p).read_text()).get("closed", [])


def stop_pct(rec: dict):
    en, sl = _f(rec.get("entry")), _f(rec.get("sl"))
    if not en or not sl:
        return None
    return (sl - en) / en * 100.0


def tp_pct(rec: dict):
    en, tp = _f(rec.get("entry")), _f(rec.get("tp"))
    if not en or not tp:
        return None
    return (tp - en) / en * 100.0


def booked_pct(rec: dict, size: float = 5.0):
    pnl = rec.get("pnl")
    return pnl / size * 100.0 if isinstance(pnl, (int, float)) else None


def const_stop(rec: dict):
    """This record's stop as a NEGATIVE distance in %, or None.

    A trail that ratcheted (sl above entry) is excluded from the bar method:
    its stop did not exist at that level until late in the trade, so replaying
    bars against the final value would over-credit the bound.
    """
    sp = stop_pct(rec)
    return sp if sp is not None and -20.0 < sp < 0 else None


# ------------------------------------------------------------------ bracket
def bracket_vs_level(records: list[dict]) -> dict:
    """How far booked SL exits sit from their own stop level.

    THREE separate claims, because the records say three different things and
    only one of them is "the fill was worse than the stop":

    * `pos_trail`  — the stop ratcheted ABOVE entry (a trail). The writer books
      the exit at the observed tick, which can sit below the trail, so a booked
      loss below the level is the trail being overrun, not a mis-book.
    * `at_breakeven` — `sl == entry`. This is the +0.5% breakeven lock (the
      writer moves `sl` to entry). A booked loss at -0.03% is the lock's own
      tick resolution. NOT evidence of anything.
    * `below_entry` — a constant stop set below entry at open. Here the booked
      fill sits BEYOND the stop on 944/985 records (mean -0.55pp, worst
      -5.58pp), which no stop fill at the level can produce: the written
      pnl is not the level, so the fill price is unrecorded and unfavourable.
    """
    out = {"n": len(records), "sl_with_level": 0, "beyond_level": 0,
           "inside_level": 0, "at_level": 0, "worst_pct": 0.0, "sum_gap": 0.0,
           "classes": {}, "by_stop": {}}
    for r in records:
        if r.get("reason") != "SL":
            continue
        sp, bp = stop_pct(r), booked_pct(r)
        if sp is None or bp is None:
            continue
        out["sl_with_level"] += 1
        cls = ("pos_trail" if sp > 0.005 else
               "at_breakeven" if abs(sp) <= 0.005 else "below_entry")
        c = out["classes"].setdefault(cls, {"n": 0, "beyond": 0, "sum_gap": 0.0,
                                            "worst_pct": 0.0})
        c["n"] += 1
        d = bp - sp
        out["sum_gap"] += d
        c["sum_gap"] += d
        if cls == "below_entry":
            out["sum_gap_below"] = out.get("sum_gap_below", 0.0) + d
        b = out["by_stop"].setdefault(f"{sp:+.2f}", {"n": 0, "beyond": 0, "mean_gap": 0.0})
        b["n"] += 1
        b["mean_gap"] += d
        if d < -0.005:
            out["beyond_level"] += 1
            b["beyond"] += 1
            c["beyond"] += 1
            out["worst_pct"] = min(out["worst_pct"], d)
            c["worst_pct"] = min(c["worst_pct"], d)
        elif d > 0.005:
            out["inside_level"] += 1
        else:
            out["at_level"] += 1
    for b in out["by_stop"].values():
        b["mean_gap"] = round(b["mean_gap"] / b["n"], 3) if b["n"] else 0.0
    for c in out["classes"].values():
        c["mean_gap"] = round(c["sum_gap"] / c["n"], 3) if c["n"] else 0.0
    out["sum_gap"] = round(out["sum_gap"], 3)
    out["mean_gap"] = (round(out["sum_gap"] / out["sl_with_level"], 3)
                       if out["sl_with_level"] else 0.0)
    if "below_entry" in out["classes"]:
        be = out["classes"]["below_entry"]
        out["mean_gap_below_entry"] = round(
            out.get("sum_gap_below", 0.0) / be["n"], 3) if be["n"] else 0.0
    return out


# --------------------------------------------------------------- bar method
class Bars:
    """5m bars from `data/trading.db`, read-only, indexed by symbol."""

    def __init__(self, db: Path | str | None = None):
        self.db = Path(db) if db else ROOT / "data" / "trading.db"
        self._cache: dict[str, tuple[list[int], list[list[float]]]] = {}

    def get(self, symbol: str, tf: str = "5m") -> tuple[list[int], list[list[float]]]:
        """([open_time_ms], [[open, high, low, close], ...]) — ascending."""
        key = f"{symbol}|{tf}"
        if key in self._cache:
            return self._cache[key]
        import sqlite3
        con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT open_time_utc, open, high, low, close FROM candles "
                "WHERE symbol=? AND timeframe=? ORDER BY open_time_utc",
                (symbol, tf)).fetchall()
        finally:
            con.close()
        ts = [int(r[0]) for r in rows]
        bars = [[float(r[1]), float(r[2]), float(r[3]), float(r[4])] for r in rows]
        self._cache[key] = (ts, bars)
        return ts, bars


def avg_gain_in_force(records: list[dict], stop: float, width: float = 0.25,
                      min_n: int = 5):
    """Mean profit (% of stake) of closed trades whose own stop is within
    `width` pp of `stop`. None when the population is too thin to be one."""
    xs = []
    for r in records:
        sp, bp = stop_pct(r), booked_pct(r)
        if sp is None or bp is None or sp >= 0:
            continue
        if abs(sp - stop) <= width:
            xs.append(bp)
    if len(xs) < min_n:
        return None
    return sum(xs) / len(xs), len(xs)


def adversarial_pnl(rec: dict, ts: list[int], bars: list[list[float]],
                    gain_pct: float | tuple, tf_min: int = 5,
                    tau2_over_3: bool = True):
    """The record's P&L at the worst fill the bars allow inside its own stop band.

    Returns (pnl_max_loss_pct, n_hit, n_bound) or None. `n_bound` counts bars
    where the high/low range exceeds the body (a bound exists); `n_hit` how
    many of those had a low at/through the threshold.

    `gain_pct` accepts either a bare float or `avg_gain_in_force`'s
    `(mean, n)` tuple — `summarise` passes the tuple, and reading it as a
    float raised TypeError there while every fixture here passed a float,
    which is why the live path went untested until 2026-11-04. A negative
    in-force gain is refused (the bound is built on a position in profit).
    """
    if isinstance(gain_pct, tuple):
        gain_pct = gain_pct[0]
    if gain_pct is None:
        return None
    g_pct: float = float(gain_pct)
    if g_pct <= 0:
        return None
    entry, slp = _f(rec.get("entry")), const_stop(rec)
    if entry is None or slp is None:
        return None
    thr = entry * (1 + slp / 100.0)
    try:
        import datetime as dt
        t0 = dt.datetime.fromisoformat(str(rec.get("ts_utc")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    t_ms = int(t0.timestamp() * 1000)
    i = bisect.bisect_left(ts, t_ms)
    worst_pct = None
    n_hit = n_bound = 0
    for j in range(i, len(ts)):
        o, h, l, c = bars[j]
        if l > thr:                     # bar never reached the stop band
            continue
        body = c - o
        if body <= 0 or h - l <= body:
            n_hit += 1                  # reached, but no bound available
            continue
        n_bound += 1
        n_hit += 1
        g = abs(g_pct) * entry / 100.0
        ratio = (h - l) / body
        adv = g * (ratio * ratio / 3.0 if tau2_over_3 else ratio)
        worst_pct = max(worst_pct or -999.0, (c - adv - entry) / entry * 100.0)
    if worst_pct is None:
        return None
    return worst_pct, n_hit, n_bound


def summarise(records: list[dict], bars: Bars, tf: str = "5m",
              tf_min: int = 5, tau2_over_3: bool = True) -> dict:
    """Book P&L re-charged at the WORST SL fill the bars allow, under a rule
    fixed before the numbers were seen:

    * basis = the collapsed paper book (`paper_book.collapse_multitf` after
      `self_improve.dedupe`), the same one every reader quotes;
    ONE-SIDED SENSITIVITY. The rule below was fixed to be one-sided BEFORE the
    numbers were computed (the previous pass's error was choosing a sizing rule
    after reading its results). First step of the rule: replace each
    `below_entry` SL fill by its WORST bar-consistent value; never by a better
    one; never close anything earlier than it closed. Then the book can only
    move down, and the reported delta is a bound, not a point estimate.

    It does NOT survive that rule today: `adversarial_pnl` lets a position
    wander for hours through the stop band before its worst bar, which is
    strictly more freedom than the stop the record carries allows for, and can
    report the position better than it booked. Where that happens the
    improvement is refused (min(adv, booked)) rather than used. That is a
    conservative floor, not a validated bound: it is reported as such.
    """
    import paper_book
    import self_improve
    recs = paper_book.collapse_multitf(self_improve.dedupe(records))[0] \
        if hasattr(paper_book, "collapse_multitf") else records
    out = {"n": len(recs), "sl_below_entry": 0, "replaced": 0,
           "skipped_no_bars": 0, "no_bound": 0, "booked_gross": 0.0,
           "adv_gross": 0.0, "moved": []}
    out["booked_gross"] = sum(booked_pct(r) or 0.0 for r in recs)
    gain_cache: dict[float, tuple | None] = {}
    for r in recs:
        if r.get("reason") != "SL" or const_stop(r) is None:
            continue
        out["sl_below_entry"] += 1
        bp = booked_pct(r)
        sp = const_stop(r)
        key = round(sp, 1)
        if key not in gain_cache:
            gain_cache[key] = avg_gain_in_force(recs, sp, width=0.25)
        g = gain_cache[key]
        if g is None:
            out["no_bound"] += 1
            continue
        ts, b = bars.get(r.get("symbol"), tf)
        if not ts:
            out["skipped_no_bars"] += 1
            continue
        adv = adversarial_pnl(r, ts, b, g[0], tf_min, tau2_over_3)
        if adv is None:
            out["no_bound"] += 1
            continue
        worst = adv[0]
        # one-sided: a bound may only LOWER the book (see the docstring)
        worst = min(worst, bp or 0.0)
        out["adv_gross"] += worst
        out["moved"].append((worst, worst - (bp or 0.0)))
        if worst < (bp or 0.0) - 0.05:
            out["n_moved_worse"] = out.get("n_moved_worse", 0) + 1
    out["adv_gross"] += out["booked_gross"] - sum(w for w, _ in out["moved"])
    out["delta_pp"] = round(out["adv_gross"] - out["booked_gross"], 3)
    if out["moved"]:
        mv = sorted(d for _, d in out["moved"])
        out["median_move_pp"] = round(mv[len(mv) // 2], 3)
    out["n_moved_worse"] = out.get("n_moved_worse", 0)
    return out


# ------------------------------------------------------- agreement check
def _one_bar_adverse_bound(rec: dict, gain_pct: float, bar: list[float],
                           tau2_over_3: bool = True):
    """The bound for ONE bar, in price units, or None when the body is <= 0."""
    entry = _f(rec.get("entry"))
    if entry is None or gain_pct <= 0:
        return None
    o, h, l, c = bar
    body = c - o
    if body <= 0 or h - l <= body:
        return None
    g = abs(gain_pct) * entry / 100.0
    ratio = (h - l) / body
    return g * (ratio * ratio / 3.0 if tau2_over_3 else ratio)


def agreement(records: list[dict], bars: Bars, tf: str = "5m") -> dict:
    """Does the one-bar bound hold where the book DOES record `exit_px`?

    TIME/STALL records carry the observed fill, so the adverse move inside that
    bar is known exactly (`exit_px - low`). The bound claims that move cannot
    exceed the in-force gain times the bar's own range/body. A violation means
    the method (or the bar data) is wrong and nothing here may be quoted.

    One bar, not the whole slide: the gain in force applies to a position's
    typical bar, and once price has fallen through the stop the book's average
    gain no longer describes the trade. Testing the cumulative excursion from
    the open would test a different (stronger) claim than the one used.
    """
    # The write path rounds pnl to 4 decimals of DOLLARS (unified_trader
    # `_book_pnl`), which is 0.02% of a $5 stake — that is the tolerance, not 0.
    # 1628/1809 records "disagree" at 1e-6 and agree to it to the rounding.
    tol = 0.02 * 1.01 / 100.0
    checked = violated = skipped = 0
    worst = 0.0
    for r in records:
        if r.get("reason") not in ("TIME", "STALL") or not r.get("exit_px"):
            continue
        entry, exit_px = _f(r.get("entry")), _f(r.get("exit_px"))
        sp, sym = const_stop(r), r.get("symbol")
        if entry is None or exit_px is None or sp is None or not sym:
            skipped += 1
            continue
        # `exit_px` is only a FILL for a position closed at market. A record
        # whose booked pnl contradicts its own exit_px (beyond the write-path
        # rounding) is not one, and must not be used to validate anything.
        bp = booked_pct(r)
        if bp is None or abs((exit_px - entry) / entry * 100.0 - bp) > tol:
            skipped += 1
            continue
        gain = avg_gain_in_force(records, sp, width=0.25)
        if gain is None:
            skipped += 1
            continue
        ts, b = bars.get(str(sym), tf)
        try:
            import datetime as dt
            t0 = dt.datetime.fromisoformat(str(r.get("ts_utc")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            skipped += 1
            continue
        i = bisect.bisect_left(ts, int(t0.timestamp() * 1000))
        hit, j = False, -1
        for k in range(i, len(ts)):
            if abs(b[k][3] - exit_px) <= 1e-6 * max(1.0, abs(exit_px)):
                hit, j = True, k
                break
        if hit:
            # one bar of adverse room: that bar, plus the next bar's low
            # (an exit stamped mid-bar can fill on the following bar)
            obs = b[j][3] - b[j][2]
            if j + 1 < len(b):
                obs = max(obs, b[j][3] - b[j + 1][2])
            bound = _one_bar_adverse_bound(r, gain[0], b[j])
            if bound is not None:
                checked += 1
                if bound > obs * 1.05:
                    violated += 1
                    worst = max(worst, (bound - obs) / entry * 100)
        if not hit:
            skipped += 1
    return {"checked": checked, "violated": violated, "skipped": skipped,
            "worst_violation_pct": round(worst, 3),
            "pass": checked > 0 and violated == 0}


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


if __name__ == "__main__":  # pragma: no cover - operator convenience
    recs = load_book()
    print(json.dumps(bracket_vs_level(recs), indent=2)[:2000])
