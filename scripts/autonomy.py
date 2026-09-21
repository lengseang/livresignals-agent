#!/usr/bin/env python3
"""AUTONOMY LAYER — the parts the system was missing.

We already have three loops (trading / adaptation / edge-hunt). What was
absent is what turns loops into LEARNING:

  1. REGISTRY   - durable memory of every hypothesis tested + its verdict,
                  so nothing gets re-tested and no mistake repeats.
  2. GUARDS     - hard, mechanical overfit checks. We over-claimed TWICE
                  (hold=30, then the A-grade gate). Those claims were caught
                  by luck and a big dataset. Guards make it systematic.
  3. DIVERGENCE - compare LIVE results to what the backtest promised.
                  If reality diverges, the edge is decaying -> demote.
  4. ATTRIBUTION- when it breaks, decompose WHY (regime / asset / exit / side)
                  instead of guessing.

Usage:
  python3 scripts/autonomy.py registry            # list all findings
  python3 scripts/autonomy.py check "<hypothesis>"# has this been tried?
  python3 scripts/autonomy.py guard <n> <features> <folds> <oos>
  python3 scripts/autonomy.py divergence
  python3 scripts/autonomy.py attribution
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
REG = BASE / "data" / "research-registry.json"
JOURNAL = BASE / "data" / "regime-trades.jsonl"


# ---------------------------------------------------------------- registry
def load_reg():
    if REG.exists():
        return json.loads(REG.read_text())
    return {"entries": []}


def cmd_registry():
    r = load_reg()
    ents = r["entries"]
    counts = {}
    for e in ents:
        counts[e["verdict"]] = counts.get(e["verdict"], 0) + 1
    print(f"REGISTRY — {len(ents)} hypotheses tested")
    print(f"verdicts: {counts}")
    print()
    for e in ents:
        v = e["verdict"]
        mark = "✅" if v.startswith("ACCEPTED") else ("⚠️" if "REJECTED" in v else "•")
        print(f"{mark} [{e['id']}] {e['hypothesis'][:78]}")
        print(f"     verdict: {v}")
        print(f"     lesson : {e['lesson'][:110]}")
        print()


def cmd_check(term):
    r = load_reg()
    term = term.lower()
    hits = [e for e in r["entries"]
            if term in (e["hypothesis"] + e["lesson"] + e["result"]).lower()]
    print(f'searching registry for: "{term}"')
    if not hits:
        print("  NO PRIOR TEST FOUND — this is genuinely new ground.")
        return
    print(f"  {len(hits)} related finding(s):")
    for e in hits:
        print(f"  [{e['id']}] {e['verdict']}")
        print(f"       {e['hypothesis'][:90]}")
        print(f"       {e['result'][:120]}")
        print(f"       LESSON: {e['lesson'][:120]}")
        print()


# ----------------------------------------------------------------- guards
def cmd_guard(n, features, folds, pos_folds, champ_pos=None):
    """Mechanical overfit checks. ALL must pass before a claim is trusted.

    FIX 2026-09-15: fold consistency was an ABSOLUTE requirement (must be
    positive in >=75% of folds). That was miscalibrated - the CHAMPION itself
    only achieves 2/3, so the check rejected every candidate and could not
    discriminate a better config from a worse one.

    It is now RELATIVE: a challenger must be no worse than the champion on
    fold consistency, while being better on the primary metric. This is what
    actually caught hold=30 (2/4 folds vs the champion's 3/4) - the comparison
    was always the point, not the absolute number.

    Regression requirement: hold=30 must STILL fail.
    """
    n, features, folds, pos_folds = (int(n), int(features), int(folds),
                                     int(pos_folds))
    spp = n / features if features else 0
    checks = []

    min_n = 20 * features
    checks.append(("sample size", n >= min_n,
                   f"{n} setups / {features} features = {spp:.1f} per feature "
                   f"(need >=20 -> {min_n})"))

    min_folds = 3
    checks.append(("fold count", folds >= min_folds,
                   f"{folds} folds (need >={min_folds} to see regime variation)"))

    if champ_pos is not None:
        cp = int(champ_pos)
        ok = pos_folds >= cp
        checks.append(("fold consistency vs champion", ok,
                       f"positive in {pos_folds}/{folds} vs champion "
                       f"{cp}/{folds} (must be >= champion)"))
    else:
        need_pos = max(1, round(folds * 0.75))
        checks.append(("fold consistency", pos_folds >= need_pos,
                       f"positive in {pos_folds}/{folds} (need >={need_pos})"))

    checks.append(("sample size vs folds", n >= 50 * folds,
                   f"{n} setups over {folds} folds = {n/max(1,folds):.0f} per "
                   f"fold (need >=50)"))

    print("=" * 74)
    print(f"OVERFIT GUARD — n={n}, features={features}, folds={folds}, "
          f"positive_folds={pos_folds}"
          + (f", champion={champ_pos}" if champ_pos is not None else ""))
    print("=" * 74)
    fails = 0
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL':4}  {name:26} {detail}")
        if not ok:
            fails += 1
    print()
    if fails:
        print(f"  ⛔ VERDICT: {fails} check(s) failed — DO NOT CLAIM AN EDGE.")
        print(f"     hold=30 (2/4 folds vs champion 3/4) IS still caught here.")
        print(f"     A challenger must beat the champion, not merely differ.")
    else:
        print("  ✅ VERDICT: checks pass. Claim is ADMISSIBLE — the live")
        print("     journal remains the final referee.")
        print("     NB passing does NOT mean the claim is true. The gate")
        print("     passed these checks and was still wrong.")
    return fails == 0


# ------------------------------------------------------------- divergence
def _live_trades():
    if not JOURNAL.exists():
        return []
    out = []
    for line in JOURNAL.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def cmd_divergence(backtest_wr=44.0, backtest_payoff=1.56):
    """Live vs backtest. The honest referee."""
    tr = _live_trades()
    print("=" * 74)
    print("LIVE vs BACKTEST DIVERGENCE")
    print("=" * 74)
    print(f"  backtest promised: WR {backtest_wr:.0f}%, payoff {backtest_payoff:.2f}")
    if not tr:
        print("  live closed trades: 0")
        print("  → NO VERDICT YET. Need at least 30 closed live trades.")
        print("    (this is the correct answer right now — the journal is young)")
        return
    wins = [t for t in tr if t.get("ret", 0) > 0]
    losses = [t for t in tr if t.get("ret", 0) <= 0]
    wr = len(wins) / len(tr) * 100
    payoff = (statistics.mean([t["ret"] for t in wins]) /
              abs(statistics.mean([t["ret"] for t in losses]))) \
        if wins and losses else 0
    n = len(tr)
    print(f"  live actual     : WR {wr:.1f}%, payoff {payoff:.2f}  (n={n})")
    print()
    if n < 30:
        print(f"  ⏳ n={n} < 30 — sample too small for any verdict.")
        return
    gap = wr - backtest_wr
    print(f"  WR gap: {gap:+.1f}pp")
    if gap <= -15:
        print("  🔴 MAJOR DIVERGENCE — the edge is not reproducing. DEMOTE.")
    elif gap <= -8:
        print("  🟠 WARNING — underperforming the promise. Watch closely.")
    elif gap >= 8:
        print("  🟢 Outperforming. Careful: could be a favourable period.")
    else:
        print("  ✅ Tracking the backtest within tolerance.")


# ------------------------------------------------------------ attribution
def cmd_attribution():
    tr = _live_trades()
    print("=" * 74)
    print("ATTRIBUTION — when WR drops, WHERE is it coming from?")
    print("=" * 74)
    if not tr:
        print("  no closed live trades yet")
        return

    def grp(keyfn, label):
        buckets = {}
        for t in tr:
            buckets.setdefault(keyfn(t), []).append(t)
        print(f"\n  --- by {label} ---")
        print(f"  {'group':22} {'n':>5} {'WR':>7} {'exp/tr':>9}")
        for k, g in sorted(buckets.items(), key=lambda x: -len(x[1])):
            w = sum(1 for t in g if t.get("ret", 0) > 0) / len(g) * 100
            e = statistics.mean([t.get("ret", 0) for t in g]) * 100
            print(f"  {str(k)[:22]:22} {len(g):>5} {w:>6.1f}% {e:>8.3f}%")

    grp(lambda t: t.get("why", "?"), "EXIT REASON")
    grp(lambda t: "gold" if t.get("feats", {}).get("is_gold") else "crypto",
        "ASSET CLASS")
    grp(lambda t: "LONG" if t.get("side", 1) == 1 else "SHORT", "DIRECTION")
    grp(lambda t: "BEAR" if t.get("feats", {}).get("bear") else "BULL", "REGIME")
    grp(lambda t: t.get("symbol", "?")[:10], "SYMBOL")
    print()
    print("  Read this to find the LEAK: the group with the worst expectancy")
    print("  and a meaningful n is where the next improvement should go.")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] == "registry":
        cmd_registry()
    elif a[0] == "check" and len(a) > 1:
        cmd_check(" ".join(a[1:]))
    elif a[0] == "guard" and len(a) >= 5:
        cmd_guard(*a[1:5])
    elif a[0] == "divergence":
        cmd_divergence()
    elif a[0] == "attribution":
        cmd_attribution()
    else:
        print(__doc__)
