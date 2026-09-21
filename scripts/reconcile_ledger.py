#!/usr/bin/env python3
"""RECONCILE ORPHANED CLOSES — repair bad references WITHOUT rewriting history.

THE PROBLEM
-----------
6 of 16 close records referenced an entry id that does not exist:

  demo-demo-DOTUSDT-20260916T130914   <- "demo-" was applied twice
  demo-SOLUSDT                        <- fallback that matched no entry

Cause: the close path in regime_trader.py re-prefixed an id that already
carried the prefix, and fell back to a bare symbol when ledger_id was unset.
(Fixed at source, but the existing records remain.)

WHY THIS MATTERS
----------------
A close whose id does not resolve is invisible to every consumer. The stats
counted it; the signal table could not show it. So the public site reported
"16 closed" while listing 10 — an internal inconsistency in a product whose
entire value is that its numbers agree with each other.

THE HARD CONSTRAINT
-------------------
The ledger is append-only and hash-chained. Rewriting a past record would
change its hash and break every link after it — destroying the exact
property we spent the whole project establishing. So this script NEVER
edits, deletes or reorders records.

Instead it appends a `reconcile` record that redeclares a close's id. The
replacement is applied when reading, so:

  - the original (wrong) record stays in the chain, forever visible
  - the correction is itself an attributed, timestamped chain entry
  - a verifier re-deriving the chain still gets a valid chain
  - the correction is auditable: you can see it happened and why

This is how a real ledger handles a clerical error, and it is strictly more
honest than a silent edit.

ONLY UNAMBIGUOUS FIXES ARE APPLIED
----------------------------------
Each candidate must resolve to EXACTLY ONE existing entry id. If a rewrite is
ambiguous or finds nothing, it is reported and skipped — never guessed. The
two genuinely orphaned closes (demo-SOLUSDT, demo-DOTUSDT) have no matching
entry, so they are left alone and surfaced for a human decision.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
sys.path.insert(0, str(BASE / "scripts"))
sys.path.insert(0, str(BASE / "src"))


def main() -> int:
    apply = "--apply" in sys.argv

    import signal_ledger as sl

    entries, closes = sl.collect_stats_inputs()
    eids = set(entries.keys())

    # ids already reconciled by a previous run
    already = {}
    for r in sl.read_records():
        if r.get("type") == "reconcile":
            p = r.get("payload") or {}
            if p.get("from") and p.get("to"):
                already[p["from"]] = p["to"]

    orphans = [c for c in closes if c.get("id") not in eids]

    print("=" * 68)
    print("RECONCILE ORPHANED CLOSES" + ("" if apply else "   (dry run)"))
    print("=" * 68)
    print(f"  entries: {len(entries)}   closes: {len(closes)}   "
          f"orphans: {len(orphans)}")
    print()

    if not orphans:
        print("  ✅ nothing to reconcile")
        return 0

    plan, unfixable = [], []
    for c in orphans:
        cid = c.get("id")

        if cid in already:
            plan.append((cid, already[cid], "already reconciled"))
            continue

        # Rule 1: a doubled prefix is an unambiguous mechanical error.
        cand = None
        if cid.startswith("demo-demo-"):
            stripped = cid.replace("demo-demo-", "demo-", 1)
            if stripped in eids:
                cand = stripped

        if cand:
            plan.append((cid, cand, "doubled 'demo-' prefix"))
        else:
            unfixable.append(cid)

    print("  PROPOSED CORRECTIONS (unambiguous, 1:1):")
    if plan:
        for old, new, why in plan:
            print(f"    {old}")
            print(f"      -> {new}   [{why}]")
    else:
        print("    (none)")
    print()

    if unfixable:
        print("  ⚠️  NO MATCHING ENTRY — leaving untouched, needs a human:")
        for cid in unfixable:
            print(f"    {cid}")
        print("      These closes have no corresponding entry record, so no")
        print("      target exists. They stay orphaned and are still counted")
        print("      in stats; decide whether to keep or annotate them.")
        print()

    if not plan:
        return 0

    if not apply:
        print("  (dry run — pass --apply to append the correction records)")
        return 0

    # ---- append reconciliation records ---------------------------------
    added = 0
    for old, new, why in plan:
        if why == "already reconciled":
            continue
        try:
            sl.append("reconcile", {
                "from": old,
                "to": new,
                "reason": why,
                "note": ("close record referenced a non-existent entry id; "
                         "original record retained unmodified in the chain"),
            })
            added += 1
        except Exception as e:
            print(f"  ⛔ failed to reconcile {old}: {str(e)[:70]}")

    print(f"  ✅ appended {added} reconcile record(s)")
    print()

    # ---- verify ---------------------------------------------------------
    v = sl.verify_chain()
    print(f"  chain after correction: "
          f"{'✅ INTACT' if v.get('valid') else '⛔ BROKEN'} "
          f"({v.get('records')} records)")

    entries2, closes2 = sl.collect_stats_inputs()
    eids2 = set(entries2.keys())
    left = [c for c in closes2 if c.get("id") not in eids2]
    print(f"  orphans before: {len(orphans)}   after: {len(left)}")
    if left:
        for c in left:
            print(f"    still orphaned: {c.get('id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
