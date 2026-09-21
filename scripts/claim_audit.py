#!/usr/bin/env python3
"""CLAIM AUDIT — every factual assertion in the site copy, against the record.

WHY THIS EXISTS
---------------
Four separate false claims have been found on the site, each written from intent
rather than from the data:

  1. "Returns are per unit of risk"              -> they were raw price returns
  2. "Profitability comes from size of winners"  -> payoff 0.76, so the opposite
  3. "Live results include [fees]"               -> no record carries a fee
  4. "Holding period: days, not minutes"         -> all 19 closed within 24h
     "Long and short"                            -> 19 of 19 were long

Every one is the kind of thing that makes a sceptical reader doubt the numbers
that ARE correct. On a product whose pitch is "verify, don't trust", a false
sentence costs more than a missing feature.

So each claim is checked here mechanically. A claim whose observed value does
not support it is returned as a failure, and the export refuses to publish the
claim text alongside it.

This is the "configured vs observed" split the spec asked for, made executable:
a strategy is CONFIGURED to trade both directions; the record OBSERVES one. Both
are true and the site should say both, rather than picking the flattering one.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def load(ledger: Path) -> tuple[list[dict], list[dict]]:
    recs = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    sigs = [r["payload"] for r in recs if r.get("type") == "signal"]
    closes = [r["payload"] for r in recs if r.get("type") == "close"]
    return sigs, closes


def build(ledger: Path) -> dict:
    sigs, closes = load(ledger)

    sides = Counter(s.get("side") for s in sigs)
    reasons = Counter(c.get("why") for c in closes)
    held = [c.get("held_days") for c in closes if c.get("held_days") is not None]

    n_long = sides.get("L", 0)
    n_short = sides.get("S", 0)
    n = len(sigs)

    # ---------------------------------------------------------------- claims
    #
    # Each entry: the claim as the site words it, whether the record supports
    # it, the observed value, and what the copy should say instead. `supported`
    # being False is not an error — it is the reason the copy has to change.
    claims = [
        {
            "id": "direction",
            "claim": "Long and short",
            "supported": n_short > 0,
            "observed": f"{n_long} long, {n_short} short across {n} signals",
            "configured": "both directions",
            "replacement": (
                f"Both directions are configured. Observed so far: "
                f"{n_long} long, {n_short} short."
                if n_short == 0 else None
            ),
        },
        {
            "id": "holding_period",
            "claim": "Holding period: days, not minutes",
            "supported": bool(held) and max(held) >= 1,
            "observed": (
                "every closed trade exited within 24 hours"
                if held and max(held) == 0
                else f"held_days range {min(held)}-{max(held)} over {len(held)} closes"
                if held else "no held_days recorded"
            ),
            "configured": "hours to days",
            "replacement": (
                "Exits are driven by momentum decay, not a clock. On this "
                "record every position closed within 24 hours, which is "
                "shorter than the strategy expects."
                if held and max(held) == 0 else None
            ),
        },
        {
            "id": "exit_reasons",
            "claim": "Exits on momentum decay or stop",
            "supported": len(reasons) > 1,
            "observed": " · ".join(f"{k}: {v}" for k, v in reasons.most_common()),
            "configured": "momentum decay and stop",
            "replacement": (
                "Every exit so far was momentum decay. No stop has been hit "
                "yet, so the stop logic is configured but unobserved."
                if len(reasons) == 1 and "mom-decay" in reasons else None
            ),
        },
    ]

    unsupported = [c for c in claims if not c["supported"]]

    return {
        "claims": claims,
        "unsupported_count": len(unsupported),
        "observed": {
            "long": n_long,
            "short": n_short,
            "total_signals": n,
            "closed": len(closes),
            "exit_reasons": dict(reasons),
            "max_held_days": max(held) if held else None,
        },
        "summary": (
            f"{len(unsupported)} of {len(claims)} stated claims are not yet "
            f"supported by the record"
            if unsupported else "every stated claim is supported by the record"
        ),
    }


if __name__ == "__main__":
    import sys

    led = Path(__file__).resolve().parents[1] / "data" / "signal-ledger.jsonl"
    audit = build(led)

    print(f"observed: {audit['observed']}")
    print()
    for c in audit["claims"]:
        mark = "✅" if c["supported"] else "⚠️ "
        print(f"  {mark} {c['id']}")
        print(f"      claim    : {c['claim']}")
        print(f"      observed : {c['observed']}")
        if c["replacement"]:
            print(f"      say this : {c['replacement'][:72]}…")
    print()
    print(f"  {audit['summary']}")
    # Never a hard failure: an unsupported claim is a copy problem, not a
    # broken ledger. Exit 0 so it can run inside the export.
    sys.exit(0)
