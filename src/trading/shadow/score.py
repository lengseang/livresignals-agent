"""Forward scorer (P2 design §6): verify every chain, then summarise each book's forward record.

    python -m trading.shadow.score --root data/local/shadow

A broken chain makes the book UNVERIFIED (no numbers are reported for it). Days containing a gap are excluded from
the statistics and listed. A book is ACCUMULATING until it has >= 60 clean forward days and >= 30 signals; then
READY_FOR_GATE (the forward gate itself, DSR and benchmarks over the same days, is P2b).
Writes <root>/scorecards.json. Read-only on the chains.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from trading.shadow.chain import utc_day, verify

MIN_DAYS, MIN_SIGNALS = 60, 30


def _records(d: Path):
    for day in sorted(d.glob("*.jsonl")):
        with open(day, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)


def score_book(d: Path) -> dict:
    v = verify(d)
    out = {"lane": d.parent.name, "strategy": d.name, "chain": v}
    if not v["ok"]:
        return {**out, "status": "UNVERIFIED", "why": v["error"]}
    marks, gap_days, signals, fills, errors = [], set(), 0, 0, []
    for r in _records(d):
        t = r.get("type")
        if t == "signal":
            signals += 1
        elif t == "shadow_fill":
            fills += 1
        elif t == "shadow_mark":
            marks.append((r["data"]["day"], float(r["data"]["equity"])))
        elif t == "gap":
            a, b = r["data"]["from_ms"], r["data"]["to_ms"]
            day = a - a % 86_400_000
            while day <= b:
                gap_days.add(utc_day(day))
                day += 86_400_000
        elif t == "error":
            errors.append(r["data"].get("error"))
    rets, clean = [], []
    for (d0, e0), (d1, e1) in zip(marks, marks[1:]):
        if d1 in gap_days or e0 <= 0:
            continue
        rets.append(e1 / e0 - 1)
        clean.append(d1)
    eq = np.cumprod([1.0] + [1 + x for x in rets])
    mdd = float(np.min(eq / np.maximum.accumulate(eq) - 1)) if len(eq) > 1 else None
    sr = (float(np.mean(rets) / np.std(rets, ddof=1)) * math.sqrt(365)
          if len(rets) >= 2 and np.std(rets, ddof=1) > 0 else None)
    status = "STOPPED" if errors else ("READY_FOR_GATE" if len(clean) >= MIN_DAYS and signals >= MIN_SIGNALS
                                        else "ACCUMULATING")
    return {**out, "status": status, "forward_days_clean": len(clean), "gap_days": sorted(gap_days),
            "signals": signals, "fills": fills, "errors": errors,
            "return": (float(eq[-1] - 1) if len(eq) > 1 else None), "max_drawdown": mdd, "sharpe_daily_ann": sr,
            "need": {"days": MIN_DAYS, "signals": MIN_SIGNALS}}


def score(root: Path | str) -> dict:
    root = Path(root)
    books = [score_book(d) for lane in sorted(root.iterdir()) if lane.is_dir()
             for d in sorted(lane.iterdir()) if d.is_dir()]
    doc = {"v": 1, "simulated": True, "label": "SHADOW (forward paper record), not evidence of edge", "books": books}
    (root / "scorecards.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return doc


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="data/local/shadow")
    a = ap.parse_args(argv)
    for b in score(a.root)["books"]:
        print(f"{b['lane']:9} {b['strategy']:22} {b['status']:15} days={b.get('forward_days_clean')} "
              f"signals={b.get('signals')}")


if __name__ == "__main__":
    main()
