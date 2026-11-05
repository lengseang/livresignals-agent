#!/usr/bin/env python3
"""Overnight probe 2026-11-05: does the paper book's net PnL cluster by session?

PRE-DECLARED DESIGN (written before any outcome was inspected):
  * Book: data/unified-paper.json 'closed', run through the production
    paper_book.collapse_multitf (SYSTEM_MAP rule #6).
  * Unit: NET pnl (pnl_net), costed records only. Uncosted records are
    dropped, never guessed (rule: USDCK is not zero).
  * Grouping key: UTC hour band of ts_utc (the entry timestamp), fixed a
    priori into four bands used elsewhere in the codebase:
        Asia   00:00-06:59, London 07:00-12:59,
        NY     13:00-20:59, Late   21:00-23:59
  * Tests: per-band mean net and 2-sample t vs the rest of the book.
  * Then: walk-forward stability — split the record set in half by time,
    report each band in each half. A band that flips sign is not a session
    effect, whatever the pooled t says.
Reported without any post-hoc regrouping.
"""
import json
import math
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from paper_book import collapse_multitf  # noqa: E402

BANDS = [("Asia", 0, 7), ("London", 7, 13), ("NY", 13, 21), ("Late", 21, 24)]


def band_of(hour: int) -> str:
    for name, lo, hi in BANDS:
        if lo <= hour < hi:
            return name
    return "?"


def welch(a, b):
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return float("nan"), float("nan")
    ma, mb = sum(a) / na, sum(b) / nb
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    se = math.sqrt(va / na + vb / nb)
    if se == 0:
        return float("nan"), ma - mb
    return (ma - mb) / se, ma - mb


def main():
    d = json.loads((ROOT / "data/unified-paper.json").read_text())
    raw = d["closed"]
    collapsed, removed = collapse_multitf(raw)
    rows = []
    uncosted = 0
    for r in collapsed:
        if r.get("pnl_net") is None:
            uncosted += 1
            continue
        ts = r.get("ts_utc")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        rows.append((dt, float(r["pnl_net"]), r.get("symbol"), r.get("edge")))
    rows.sort(key=lambda x: x[0])
    print(f"raw {len(raw)} -> collapsed {len(collapsed)} (removed {removed}); "
          f"costed {len(rows)}, uncosted dropped {uncosted}")
    print(f"span {rows[0][0].date()} .. {rows[-1][0].date()}\n")

    total = [p for _, p, _, _ in rows]
    mean_all = sum(total) / len(total)
    print(f"WHOLE BOOK n={len(total)} net={sum(total):+.4f} mean={mean_all:+.5f}")
    print(f"{'band':8}{'n':>5}{'net':>10}{'mean':>10}{'t vs rest':>11}")

    def band_stats(sub):
        out = {}
        for name, _, _ in BANDS:
            vals = [p for dt, p, _, _ in sub if band_of(dt.hour) == name]
            rest = [p for dt, p, _, _ in sub if band_of(dt.hour) != name]
            t, diff = welch(vals, rest)
            out[name] = (len(vals), sum(vals), sum(vals) / len(vals) if vals else float("nan"), t)
        return out

    pooled = band_stats(rows)
    for name, _, _ in BANDS:
        n, net, mean, t = pooled[name]
        print(f"{name:8}{n:>5}{net:>+10.3f}{mean:>+10.5f}{t:>+11.2f}")

    half = len(rows) // 2
    print(f"\nWALK-FORWARD (split at index {half}, {rows[half][0].date()})")
    for label, sub in (("H1", rows[:half]), ("H2", rows[half:])):
        st = band_stats(sub)
        parts = [f"{k} n={st[k][0]} m={st[k][2]:+.4f} t={st[k][3]:+.2f}"
                 for k, _, _ in BANDS]
        print(f" {label}: " + " | ".join(parts))

    # how many distinct calendar days carry the book at all
    days = {}
    for dt, p, _, _ in rows:
        days.setdefault(dt.date(), []).append(p)
    print(f"\ndistinct days {len(days)}; "
          f"largest day net {max(sum(v) for v in days.values()):+.3f}")


if __name__ == "__main__":
    main()
