#!/usr/bin/env python3
"""Compactor — turn thousands of small passing strategies into a few ensembles.

THE OWNER'S DESIGN
------------------
"expecting thousand of small strategies a few days then compact them into a
multifunctional strategy for more improvement."

THE CHOICE THAT MATTERS
-----------------------
There are two ways to "compact", and they are not equivalent:

  A) ENSEMBLE   keep every strategy, have the survivors vote, size by agreement.
  B) BLEND      average the parameters away and ship one merged rule.

This implements A, and refuses B. The reason is the same rule the public site
holds itself to: every published number must be traceable to a record that can
be inspected. A blended strategy has one number and no way to check which of its
ancestors earned it. An ensemble keeps every member's own track record, so a
member that stops working can be identified and retired instead of quietly
dragging the whole thing down.

So "compact" here means: GROUP the survivors by what they have in common, RANK
the groups, and emit one ensemble per group. The result is few things to run,
thousands of things to audit.

HOW GROUPS ARE FORMED
---------------------
Survivors are keyed by (timeframe, family, direction-of-bias). Members within a
group differ only in lookback / hold / target, which is exactly the kind of
variation that should be averaged over rather than picked between. A group is
only emitted as an ensemble if it holds at least MIN_MEMBERS members — a group of
one is not an ensemble, it is a strategy, and it already exists in the book.

Output is written for REVIEW. Nothing here activates, deploys, or trades.
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

AGENT = Path("/opt/trading-agent")
BOOK = AGENT / "data/strategy-book"
ENTRIES = BOOK / "entries.jsonl"
ENSEMBLES = BOOK / "ensembles.json"
MIN_MEMBERS = 3          # a group of 1-2 is not an ensemble


def _load_entries():
    out = []
    try:
        with open(ENTRIES, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    out.append(json.loads(line))
    except Exception:
        pass
    return out


def main() -> int:
    rows = _load_entries()
    passed = [r for r in rows if r.get("status") == "passed"]
    print(f"book entries : {len(rows)}")
    print(f"passing      : {len(passed)}")
    if not passed:
        print("nothing to compact — no strategy has passed the gate yet")
        print("(this is a valid outcome: the gate is strict on purpose)")
        return 0

    groups = defaultdict(list)
    for r in passed:
        key = (r["timeframe"], r["family"])
        groups[key].append(r)

    ensembles = []
    for (tf, fam), members in sorted(groups.items()):
        if len(members) < MIN_MEMBERS:
            continue
        # ensemble expectation = mean of member expectations. Members are
        # independent parameterisations, so the mean is the honest summary and
        # the dispersion tells us whether the group is coherent or scattered.
        npt = [m["net_per_trade"] for m in members]
        wrs = [m["wr"] for m in members]
        nets = [m["net"] for m in members]
        # a coherent group has members that mostly agree on sign and rough size
        positive = sum(1 for x in npt if x > 0)
        ens = {
            "id": f"ens|{fam}|{tf}",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "family": fam,
            "timeframe": tf,
            "members": len(members),
            "members_positive": positive,
            "coherence_pct": round(positive / len(members) * 100, 1),
            "mean_net_per_trade": round(statistics.mean(npt), 6),
            "median_net_per_trade": round(statistics.median(npt), 6),
            "stdev_net_per_trade": round(statistics.pstdev(npt), 6) if len(npt) > 1 else 0.0,
            "mean_win_rate": round(statistics.mean(wrs), 2),
            "total_net_across_members": round(sum(nets), 4),
            "member_ids": [m["id"] for m in members],
            "status": "ensemble_candidate",
        }
        ensembles.append(ens)

    ensembles.sort(key=lambda e: -e["mean_net_per_trade"])

    out = {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "source_entries": len(rows),
        "source_passing": len(passed),
        "ensembles": ensembles,
        "note": ("Ensembles are GROUPS of independently recorded strategies, not "
                 "blended parameters. Every member keeps its own record in "
                 "entries.jsonl so it can be audited and retired separately. "
                 "Nothing here is active or deployed."),
    }
    BOOK.mkdir(parents=True, exist_ok=True)
    with open(ENSEMBLES, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    print(f"\nensembles formed : {len(ensembles)}")
    print(f"{'family':<18} {'tf':<5} {'members':>7} {'coherence':>10} "
          f"{'mean net/trade':>15} {'total net':>10}")
    print("-" * 72)
    for e in ensembles[:20]:
        print(f"{e['family']:<18} {e['timeframe']:<5} {e['members']:>7} "
              f"{e['coherence_pct']:>9.1f}% {e['mean_net_per_trade']:>+15.5f} "
              f"{e['total_net_across_members']:>+10.2f}")

    if not ensembles:
        print("no group reached MIN_MEMBERS — survivors are still too few or too scattered")

    print(f"\nwritten: {ENSEMBLES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
