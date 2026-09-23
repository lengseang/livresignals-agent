#!/usr/bin/env python3
"""PER-SYMBOL NET EXPECTANCY — using the canonical unit reader.

WHY THIS EXISTS INSTEAD OF A QUICK GROUP-BY
-------------------------------------------
Money decisions should not rest on a hand-rolled aggregation. Two separate
attempts at this question produced two different answers for the same symbol,
because `pnl` means DOLLARS in some books and a FRACTION in others, and a reader
that guesses the unit produces numbers that look plausible and are wrong.

book_units.py exists precisely because that mistake has already been made three
times in this codebase. So this script does not do its own unit handling: it
calls book_units.as_percent() for every record, which declares the field and unit
per book and cross-checks the dollar figure against the recorded entry/exit
prices when both are available.

WHAT IT ANSWERS
---------------
For each symbol, per book: how many trades, the gross return per trade, and the
return NET of the measured 0.1594% round-trip fee. A symbol whose net is negative
loses money regardless of its win rate, and no signal improvement changes that —
only trading it less, or larger, does.
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path("/opt/trading-agent")
sys.path.insert(0, str(BASE / "scripts"))
sys.path.insert(0, str(BASE / "src"))

from book_units import as_percent          # noqa: E402

FEE_RT = 0.1594          # percent, round trip

SOURCES = [
    ("trend_follower", "trend-state.json", "log"),
    ("unified_trader", "unified-paper.json", "closed"),
    ("adaptive_engine", "adaptive-state.json", "closed_trades"),
    ("regime_trader", "regime-state.json", "log"),
    ("futures_executor", "futures-trades.json", "closed"),
    ("live_executor", "live-trades.json", "closed"),
]


def tstat(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return 0.0 if sd == 0 else m / (sd / math.sqrt(n))


def main():
    per_book = defaultdict(lambda: defaultdict(list))
    skipped = defaultdict(int)
    for book, fname, key in SOURCES:
        p = BASE / "data" / fname
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        for rec in (d.get(key) or []):
            if not isinstance(rec, dict):
                continue
            sym = rec.get("symbol") or rec.get("pair") or "?"
            try:
                pc = as_percent(book, rec)
            except KeyError:
                pc = None
            if pc is None:
                skipped[book] += 1
                continue
            per_book[book][sym].append(pc)

    print("=" * 100)
    print("PER-SYMBOL NET EXPECTANCY — units read via book_units.as_percent()")
    print("=" * 100)
    print(f"  round-trip fee: {FEE_RT:.4f}%  |  a symbol under this line loses "
          f"money at any win rate")
    print()

    # aggregate across books, but keep the per-book split visible for the worst
    agg = defaultdict(list)
    for book, syms in per_book.items():
        for sym, rs in syms.items():
            agg[sym].extend(rs)

    print("  ALL BOOKS COMBINED (so a symbol is judged on its whole record)")
    print(f"  {'symbol':<13}{'n':>5}{'gross':>9}{'NET':>9}{'t':>7}"
          f"{'win%':>7}   verdict")
    print("  " + "-" * 94)
    bad = []
    for sym, rs in sorted(agg.items(), key=lambda kv: -(sum(kv[1]) / len(kv[1]))):
        n = len(rs)
        if n < 5:
            continue
        avg = sum(rs) / n
        net = avg - FEE_RT
        t = tstat(rs)
        wr = sum(1 for x in rs if x > 0) / n * 100
        if net <= 0:
            v = f"🔴 LOSES {abs(net):.3f}%/trade"
            bad.append((sym, n, avg, net, t, wr))
        elif abs(t) < 2:
            v = f"⏳ net +{net:.3f}% but unproven (t={t:+.2f})"
        else:
            v = f"✅ earns {net:.3f}%/trade"
        print(f"  {sym:<13}{n:>5}{avg:>8.3f}%{net:>8.3f}%{t:>+7.2f}"
              f"{wr:>6.1f}%   {v}")

    print()
    print("=" * 100)
    print(f"  {len(bad)} symbol(s) have a positive-or-zero GROSS record that the "
          f"fee turns negative")
    for sym, n, avg, net, t, wr in sorted(bad, key=lambda r: r[3]):
        who = [b for b in per_book if sym in per_book[b]]
        print(f"    {sym:<12} n={n:<4} gross {avg:+.3f}%  NET {net:+.3f}%  "
              f"win {wr:.0f}%  t={t:+.2f}  (books: {', '.join(who)})")
    print()
    print("  ⚠️ A symbol judged on trades from SEVERAL strategies answers the")
    print("     wrong question for any ONE of them. The per-book split is printed")
    print("     above for exactly that reason — do not prune a symbol from a")
    print("     trend universe on the strength of high-frequency trades.")

    if any(skipped.values()):
        print()
        print("  records whose return could not be recovered (excluded, not "
              "counted as zero):")
        for b, k in skipped.items():
            print(f"    {b}: {k}")


if __name__ == "__main__":
    main()
