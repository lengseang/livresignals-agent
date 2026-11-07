"""Is the gateway's `probabilities` block a score, or a decision constraint?

READ-ONLY. Source: /opt/trading-agent-v2 pipeline records, both paper lanes.
No writes outside stdout, no orders, no state.

Why the question matters: every V2 entry decision is `proposal` + `policy`,
and the gateway answer that feeds them carries `action`, `confidence` and a
`probabilities` block over LONG/SHORT/HOLD/EXIT/REDUCE/NO_TRADE. If those six
numbers are a ranking, then LONG's probability should *predict* an entry's
return. If the action is always the argmax and the other five slots are pinned
to constants, the block is a decision label expressed as six numbers and
nothing downstream can rank candidates by it.

Reported:
  1. how often `action` == argmax(probabilities)
  2. the distinct value of each slot, and how often each occurs
  3. whether a LONG-probability spread exists across symbols
"""
from __future__ import annotations

import glob
import json
from collections import Counter, defaultdict

LANES = ("paper", "paper-b")
SLOTS = ("LONG", "SHORT", "HOLD", "EXIT", "REDUCE", "NO_TRADE")


def load():
    for lane in LANES:
        for f in sorted(glob.glob(f"/opt/trading-agent-v2/data/v2/{lane}/v2cycle/pipeline/*.jsonl")):
            for ln in open(f):
                r = json.loads(ln)
                if "decision" in r:
                    r["_lane"] = lane
                    yield r


def main() -> None:
    rows = list(load())
    print(f"records with a decision: {len(rows)}")
    if not rows:
        return

    argmax_hits = 0
    slot_values = {s: Counter() for s in SLOTS}
    by_symbol = defaultdict(list)
    by_action = Counter()
    src = Counter()
    for r in rows:
        d = r["decision"]
        p = d.get("probabilities") or {}
        act = d.get("action")
        if p:
            top = max(p, key=p.get)
            argmax_hits += top == act
        for s in SLOTS:
            if s in p:
                slot_values[s][round(float(p[s]), 6)] += 1
        by_symbol[r.get("symbol")].append(p.get("LONG"))
        by_action[act] += 1
        src[d.get("source")] += 1

    print(f"action == argmax(probabilities): {argmax_hits}/{len(rows)} "
          f"({100.0 * argmax_hits / len(rows):.1f}%)")
    print(f"decision sources: {dict(src)}")
    print(f"actions: {dict(by_action)}")
    print()
    print("distinct values per slot (count of distinct, then the 3 most common):")
    for s in SLOTS:
        c = slot_values[s]
        common = ", ".join(f"{v:g}×{n}" for v, n in c.most_common(3))
        print(f"  {s:9s} distinct={len(c):4d}  {common}")
    print()
    print("LONG probability per symbol (is there a spread to rank on?):")
    for sym, vals in sorted(by_symbol.items()):
        vals = [v for v in vals if isinstance(v, (int, float))]
        if not vals:
            continue
        vals_sorted = sorted(vals)
        mean = sum(vals) / len(vals)
        p10 = vals_sorted[len(vals) // 10]
        p90 = vals_sorted[(len(vals) * 9) // 10]
        print(f"  {sym:10s} n={len(vals):5d} mean={mean:.4f} p10={p10:.4f} p90={p90:.4f} "
              f"spread={p90 - p10:.4f}")


if __name__ == "__main__":
    main()
