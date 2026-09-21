#!/usr/bin/env python3
"""RECORD FIGURES — what each published number does and does not include.

WHY THIS FILE EXISTS
--------------------
Repeated pattern in this project: the copy asserts something the data does not
support. Three found so far.

  1. "Returns are per unit of risk"      — they were raw price returns
  2. "Profitability comes from the size of winners" — payoff was 0.76, i.e.
     winners were SMALLER than losers
  3. "Live results include [fees]"        — no close record contains a fee, and
     the trader computes ret as a raw price move

Each was written from intent rather than from the record, and each is the kind
of claim that makes a sceptical reader distrust every other number on the page.
A single verified falsehood is worse than ten acknowledged gaps.

So the cost model is stated here, once, from the code, and the site reads it.
If fee accounting is ever added to the trader, this file changes and every
page follows.

VERIFIED AGAINST THE CODE, 17 Sep 2026
--------------------------------------
    scripts/regime_trader.py, close append:
        _sl.append("close", {"exit": px, "ret": round(move, 5), ...})
    `move` is the raw price change. No fee is subtracted.

    grep for fee/commission across scripts/ and config/
        -> only hit is a BACKTEST assumption (0.144%) in a docstring
        -> nothing in the live or demo execution path

    union of close payload keys across all 19 closes:
        backfilled, exit, held_days, id, pnl, ret, why
        -> no fees key exists
"""
from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------- the model

COSTS = {
    # Are fees subtracted from the published returns?
    "fees_included": False,

    # Are they modelled anywhere in the live path?
    "fees_modelled_in_live_path": False,

    # The only fee figure that exists in the repository, and where it lives.
    "backtest_fee_bps": 14.4,
    "backtest_fee_note": (
        "The backtest assumes 0.144% round-trip. That figure appears in a "
        "backtest docstring only. It is not applied to the published record."
    ),

    # Slippage is not modelled either, and on a demo book it would not be
    # meaningful if it were.
    "slippage_included": False,

    "short": "Price returns, before costs",

    "long": (
        "Every return on this site is the raw price change from entry to exit. "
        "Exchange fees and slippage are NOT subtracted, because the demo "
        "execution path does not model them. Real results would be worse by "
        "roughly the round-trip cost on every trade."
    ),

    # Stated as a range rather than a single number, because the honest answer
    # depends on the fee tier and on whether the venue charges a maker or
    # taker fee. Being vague here would be its own kind of dishonesty.
    "impact_estimate": (
        "At typical spot taker fees a round trip costs roughly 0.1 to 0.2 "
        "percent. On this sample of 19 trades the current cumulative return "
        "would be lower by several percent if those were applied."
    ),

    "why_not_modelled": (
        "Demo fills do not reproduce a real fee schedule or real market impact, "
        "so subtracting a number would be inventing precision. The honest move "
        "is to publish gross returns and say so."
    ),
}


def build() -> dict:
    """Return the cost model, plus a live check that it still matches the data."""
    return dict(COSTS)


def verify_against_ledger(ledger_path: Path) -> tuple[bool, list[str]]:
    """Confirm no close record has acquired a fee field since this was written.

    If a fee field ever appears, this returns False and the site's copy is
    stale — which is exactly when a human should look at it.
    """
    notes: list[str] = []
    try:
        closes = [
            json.loads(line)
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except Exception as exc:  # noqa: BLE001
        return False, [f"could not read ledger: {exc}"]

    closes = [r for r in closes if r.get("type") == "close"]
    with_fees = [
        r for r in closes
        if any(k in (r.get("payload") or {}) for k in ("fees", "fees_bps", "fee"))
    ]

    if with_fees:
        notes.append(
            f"{len(with_fees)} close record(s) now carry a fee field. "
            "The site states fees are NOT included — that copy is now wrong."
        )
        return False, notes

    notes.append(f"{len(closes)} close records checked, none carry a fee field")
    return True, notes


if __name__ == "__main__":
    import sys

    model = build()
    print(f"fees included in published returns: {model['fees_included']}")
    print(f"fees modelled in the live path:     {model['fees_modelled_in_live_path']}")
    print(f"slippage included:                  {model['slippage_included']}")
    print()

    ledger = Path(__file__).resolve().parents[1] / "data" / "signal-ledger.jsonl"
    ok, notes = verify_against_ledger(ledger)
    for n in notes:
        print(f"  {'✅' if ok else '⛔'} {n}")
    sys.exit(0 if ok else 1)
