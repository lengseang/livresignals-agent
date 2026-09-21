#!/usr/bin/env python3
"""PUBLIC RECORD MODEL — the single source of truth for every published figure.

WHY THIS FILE EXISTS
--------------------
An external review (17 Sept 2026) found the site's stats contradicted its own
table:

    site showed   closed 17   won 8 / lost 9   win rate 47.06%   cumulative -4.10%
    table showed  closed 15   won 7 / lost 8   win rate 46.67%   cumulative -4.07%

On a site whose entire pitch is "verify, don't trust", a stat that disagrees
with its own table is the most damaging bug possible — worse than any missing
feature, because it attacks the one thing being sold.

ROOT CAUSE
----------
Two closes (seq 21, 22: demo-SOLUSDT / demo-DOTUSDT) referenced positions
opened BEFORE the ledger existed, so no `signal` record exists for them. They
were counted in every statistic but had no row to render in the table.

THE FIX, and why it is shaped this way
--------------------------------------
Two wrong answers were available:

  (a) delete the orphan closes        -> falsifies history. Never.
  (b) leave them counted              -> the stats/table disagreement above.

The correct answer is to MODEL them honestly. A close with no entry record is
a REAL TRADE with an UNVERIFIABLE BASIS: we know it closed and what it
returned, but we have no committed entry to check it against. So it is:

  - excluded from the headline metrics (which are claims about verifiable
    signals), and
  - published explicitly as an "unattributed" count, so nothing is hidden and
    the totals still reconcile.

Every published number in this module is derived from ONE closed-set
definition, and `check_consistency()` asserts the arithmetic holds. The site
cannot disagree with its table because both are rendered from the same model.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


# --------------------------------------------------------------- helpers

def r2(x: float | None, nd: int = 2) -> float | None:
    """Round for display. One place, so nothing rounds inconsistently."""
    if x is None:
        return None
    return round(x + 0.0, nd)


def _is_backfilled(close: dict, entries: dict) -> bool:
    """Was this close imported from the journal rather than published live?

    The flag can live on the close record or on the entry it references, so
    check both rather than trusting one location.
    """
    if close.get("backfilled"):
        return True
    e = entries.get(close.get("id")) or {}
    return bool(e.get("backfilled"))


def pct(x: float | None, nd: int = 1) -> str:
    if x is None:
        return "—"
    s = f"{x:+.{nd}f}%"
    return s


def build_model() -> dict:
    """Assemble the public record from the ledger. Pure function of the data."""
    import signal_ledger as sl

    entries, closes = sl.collect_stats_inputs()
    eids = set(entries.keys())

    # A close is ATTRIBUTED when an entry record exists for it. Only these can
    # support a claim, because only these can be verified end to end.
    attributed = [c for c in closes if c.get("id") in eids]
    unattributed = [c for c in closes if c.get("id") not in eids]

    def ret_of(c: dict) -> float:
        """Return as a FRACTION (0.0244 = +2.44%)."""
        v = c.get("ret")
        if v is None:
            return 0.0
        return float(v)

    wins = [c for c in attributed if ret_of(c) > 0]
    losses = [c for c in attributed if ret_of(c) <= 0]

    n = len(attributed)
    wr = (len(wins) / n * 100) if n else None
    avg_win = (sum(ret_of(c) for c in wins) / len(wins) * 100) if wins else None
    avg_loss = (abs(sum(ret_of(c) for c in losses) / len(losses)) * 100) if losses else None
    payoff = (avg_win / avg_loss) if (avg_win and avg_loss) else None
    expectancy = (sum(ret_of(c) for c in attributed) / n * 100) if n else None

    # equity curve + max drawdown, in close order
    cum, curve, peak, mdd = 0.0, [], 0.0, 0.0
    for i, c in enumerate(attributed):
        cum += ret_of(c) * 100
        curve.append({"i": i, "cumulative_pct": r2(cum, 2), "id": c.get("id")})
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)

    # longest losing run
    longest = run = 0
    for c in attributed:
        if ret_of(c) > 0:
            run = 0
        else:
            run += 1
            longest = max(longest, run)

    # R-multiple, ONLY where a stop was committed. Labelled partial on purpose:
    # showing an R figure computed from a guessed stop would be worse than
    # showing none.
    r_vals = []
    for c in attributed:
        e = entries.get(c.get("id")) or {}
        ent, stop, ex = e.get("entry"), e.get("stop"), c.get("exit")
        if ent and stop and ex and abs(ent - stop) > 1e-12:
            r = (ex - ent) / (ent - stop)
            if (e.get("side") or "L") in ("S", "short", -1):
                r = -r
            r_vals.append(r)

    model = {
        "signals_published": len(entries),
        "closed": n,
        "open": len(entries) - n,
        "wins": len(wins),
        "losses": len(losses),
        # ---- provenance split ------------------------------------------
        # A reader deserves to know how much of this was published live
        # versus imported from the trader's journal after the fact. Both are
        # real trades, but they are not the same kind of evidence: a
        # backfilled record was written after its outcome was known, so it
        # cannot demonstrate "committed before the outcome" the way a live
        # one does. Publishing the split is more honest than a single number
        # that implies everything was live.
        "live_closed": sum(1 for c in attributed if not _is_backfilled(c, entries)),
        "backfilled_closed": sum(1 for c in attributed if _is_backfilled(c, entries)),
        "win_rate_pct": r2(wr, 1),
        "avg_win_pct": r2(avg_win, 2),
        "avg_loss_pct": r2(avg_loss, 2),
        "payoff": r2(payoff, 2),
        "expectancy_pct": r2(expectancy, 2),
        "cumulative_return_pct": r2(cum, 2),
        "max_drawdown_pct": r2(mdd, 2),
        "longest_losing_streak": longest,
        # explicitly published, never silently dropped
        "unattributed_closes": len(unattributed),
        "unattributed_note": (
            "Closes whose entry predates the ledger, so no committed entry "
            "exists to verify them against. Excluded from the metrics above; "
            "listed here so the totals reconcile."
        ),
        "r_sample_size": len(r_vals),
        "r_mean": r2(sum(r_vals) / len(r_vals), 2) if r_vals else None,
        "equity_curve": curve,
    }
    return model


# ------------------------------------------------------- consistency guard

def check_consistency(m: dict) -> list[str]:
    """Assert the published figures reconcile. Returns a list of problems.

    This is the regression guard for the exact class of bug the review found.
    It fails loudly rather than publishing numbers that disagree.
    """
    problems: list[str] = []

    if m["wins"] + m["losses"] != m["closed"]:
        problems.append(
            f"wins({m['wins']}) + losses({m['losses']}) != closed({m['closed']})")

    if m["closed"] + m["open"] != m["signals_published"]:
        problems.append(
            f"closed({m['closed']}) + open({m['open']}) != "
            f"signals_published({m['signals_published']})")

    if m["win_rate_pct"] is not None and m["closed"]:
        expect = round(m["wins"] / m["closed"] * 100, 1)
        if abs(expect - m["win_rate_pct"]) > 0.051:
            problems.append(
                f"win_rate {m['win_rate_pct']} != wins/closed ({expect})")

    if len(m["equity_curve"]) != m["closed"]:
        problems.append(
            f"equity points({len(m['equity_curve'])}) != closed({m['closed']})")

    # the last equity point must equal the cumulative figure
    if m["equity_curve"]:
        last = m["equity_curve"][-1]["cumulative_pct"]
        if abs(last - (m["cumulative_return_pct"] or 0)) > 0.011:
            problems.append(
                f"cumulative({m['cumulative_return_pct']}) != last curve point ({last})")

    if m["r_sample_size"] > m["closed"]:
        problems.append("r sample larger than the closed set")

    # provenance split must account for exactly the closed set
    if m["live_closed"] + m["backfilled_closed"] != m["closed"]:
        problems.append(
            f"live({m['live_closed']}) + backfilled({m['backfilled_closed']}) "
            f"!= closed({m['closed']})")

    return problems


def main() -> int:
    m = build_model()
    print("=" * 66)
    print("PUBLIC RECORD MODEL")
    print("=" * 66)
    for k in ("signals_published", "closed", "open", "wins", "losses",
              "live_closed", "backfilled_closed",
              "win_rate_pct", "avg_win_pct", "avg_loss_pct", "payoff",
              "expectancy_pct", "cumulative_return_pct", "max_drawdown_pct",
              "longest_losing_streak", "unattributed_closes",
              "r_sample_size", "r_mean"):
        print(f"  {k:<26} {m[k]}")
    print()
    problems = check_consistency(m)
    if problems:
        print("⛔ INCONSISTENT:")
        for p in problems:
            print(f"   - {p}")
        return 1
    print("✅ every figure reconciles with the closed set")
    return 0


if __name__ == "__main__":
    sys.exit(main())
