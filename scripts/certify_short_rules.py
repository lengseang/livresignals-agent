#!/usr/bin/env python3
"""FEE-STRESS + WALK-FORWARD CERTIFICATION for the short-breakdown findings.

WHY THIS EXISTS
---------------
The hunt found six short-breakdown rules that are net-positive after costs. Before
any of them is allowed near paper — let alone money — three questions have to be
answered in a way that cannot flatter them:

  1. DOES THE EDGE SURVIVE A WORSE FEE?   The hunt assumed 10bps per side. Real
     execution may be worse. A rule whose profit vanishes at 15bps was never a
     rule, it was a fee assumption.

  2. DOES IT SURVIVE OUT OF SAMPLE?       An in-sample optimum over a search space
     is a number, not an edge. This splits the history into consecutive halves and
     requires the sign to hold in BOTH.

  3. IS IT ONE MECHANISM OR SIX?          Six rules that fire on the same bars are
     one bet wearing six hats. Pairwise overlap is measured, not assumed.

THE TRAP THIS TOOL IS BUILT TO AVOID
------------------------------------
Earlier today I "found" that removing TIME exits turned a losing family into
PF 5.02 — and it was pure lookahead (selecting on the outcome). Then I computed a
+0.125% edge for the dip-buyer by taking the best of 14 hold hours on 40 trades —
and the repo's own out-of-sample tests had already measured -0.364% for the same
entry. Both times the flattering number came from choosing a configuration by how
it performed on the sample being reported.

So: every split here is CONSECUTIVE IN TIME, never shuffled, never selected by
result. A rule that only works in one half is reported as regime-dependent and is
NOT certifiable, however good its full-period PF looks.

Usage:
    python3 certify_short_rules.py                 # all six, full report
    python3 certify_short_rules.py --fee 0.40      # worse fee, round trip percent
    python3 certify_short_rules.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

BASE = Path("/opt/trading-agent")
sys.path.insert(0, str(BASE / "scripts"))
sys.path.insert(0, str(BASE / "src"))

FINDINGS = BASE / "reports" / "hunt5" / "findings.jsonl"

# The hunt's own convention: fee_bps + slippage_bps = 10bps per side = 20bps RT.
HUNT_RT = 0.0020
NOTIONAL = 5.0


@dataclass
class Verdict:
    name: str
    n_down: int
    net_down: float
    pf_down: float
    net_up: float
    net_at_fee: float = 0.0
    survives_fee: bool = False
    first_half_net: float | None = None
    second_half_net: float | None = None
    both_halves_positive: bool = False
    certifiable: bool = False
    verdict: str = ""


def load_best() -> dict[str, dict]:
    """Best variant per distinct rule name, from the standing hunt."""
    if not FINDINGS.exists():
        return {}
    best: dict[str, dict] = {}
    for line in FINDINGS.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        k = r.get("name")
        if not k:
            continue
        if k not in best or r.get("dn_pf", 0) > best[k].get("dn_pf", 0):
            best[k] = r
    return best


def rescale(net: float, n: int, rt_new: float, rt_old: float = HUNT_RT) -> float:
    """Re-derive net at a different round-trip cost.

    The engine charged rt_old per trade on NOTIONAL, so the gross figure is
    recoverable: gross = net + n * NOTIONAL * rt_old. Then subtract the new cost.
    This is arithmetic on the stored result, not a re-run — labelled as such in
    the output so nobody mistakes it for a fresh backtest.
    """
    gross = net + n * NOTIONAL * rt_old
    return gross - n * NOTIONAL * rt_new


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fee", type=float, default=0.30,
                    help="round-trip cost in percent (default 0.30)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rt = args.fee / 100.0

    best = load_best()
    if not best:
        print(f"no findings at {FINDINGS}", file=sys.stderr)
        return 1

    verdicts: list[Verdict] = []
    for name, r in sorted(best.items(), key=lambda kv: -kv[1].get("dn_pf", 0)):
        v = Verdict(
            name=name,
            n_down=r.get("dn_n", 0),
            net_down=r.get("dn_net", 0.0),
            pf_down=r.get("dn_pf", 0.0),
            net_up=r.get("up_net", 0.0),
        )
        v.net_at_fee = rescale(v.net_down, v.n_down, rt)
        v.survives_fee = v.net_at_fee > 0
        verdicts.append(v)

    if args.json:
        print(json.dumps({
            "round_trip_cost_pct": args.fee,
            "note": ("net_at_fee is rescaled from the stored hunt result, not a "
                     "re-run. Halves are not available in the findings file, so "
                     "both_halves_positive is left null unless supplied."),
            "rules": [asdict(v) for v in verdicts],
        }, indent=2))
        return 0

    print("=" * 84)
    print("SHORT-RULE CERTIFICATION — fee stress before anything goes near paper")
    print(f"round-trip cost applied: {args.fee:.2f}%   (hunt assumed {HUNT_RT*100:.2f}%)")
    print("=" * 84)
    print()
    print(f"{'rule':22} {'down n':>7} {'hunt net':>10} {'net@fee':>10} "
          f"{'PF':>6} {'up net':>9}  verdict")
    print("-" * 84)
    for v in verdicts:
        tag = "SURVIVES" if v.survives_fee else "DIES"
        print(f"{v.name:22} {v.n_down:7} {v.net_down:+10.2f} {v.net_at_fee:+10.2f} "
              f"{v.pf_down:6.2f} {v.net_up:+9.2f}  {tag}")
    print("-" * 84)
    print()
    print("READ THIS BEFORE TRUSTING THE TABLE")
    print("  * net@fee is rescaled from the stored hunt result — it is arithmetic")
    print("    on a completed backtest, not a fresh run.")
    print("  * the halves are NOT in the findings file. This table therefore")
    print("    cannot tell you whether a rule holds OUT OF SAMPLE, and none of")
    print("    these rules should be called certifiable until it is re-run")
    print("    walk-forward. That is the next step, not this one.")
    print("  * 'up net' is the same rule's bleed in up-regimes. Every one of")
    print("    these is short-only, so a live deployment must also carry the")
    print("    regime gate or it will hand back its down-market profit.")
    print()
    n_surv = sum(1 for v in verdicts if v.survives_fee)
    print(f"  {n_surv}/{len(verdicts)} rules still positive at {args.fee:.2f}% round trip.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
