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

TWO BOOKS IN ONE FILE (T0.10/T0.11)
-----------------------------------
`trend-state.json` holds a PAPER universe (`log`, 53 symbols, candle-close
exits) and the REAL money book (`real.closed`). They are reported separately:
the paper universe under `trend_paper_universe`, the real book in DOLLARS with
its UNKNOWN closes counted, never zeroed. Before T0.11 the paper `log` was read
under the name `trend_follower` and the paper universe was presented as the
trend book.
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

# (book, file, key-path). The key-path is a tuple so a book nested inside
# another (the trend REAL money book lives at real.closed) is addressed
# explicitly rather than flattened into a name that lies about what it is.
#
# T0.11: `trend-state.json` holds TWO different books. `log` is the 53-symbol
# PAPER universe (candle-close exits, no money) and must be named as paper, not
# as "trend_follower". The trend book that holds real money is `real.closed`,
# read through the `real_closed` book (pnl_usd = NET DOLLARS, or UNKNOWN).
# Before this, one entry read the paper `log` under the name `trend_follower`
# and the paper universe was reported as the trend book.
SOURCES = [
    ("trend_paper_universe", "trend-state.json", ("log",)),
    ("real_closed", "trend-state.json", ("real", "closed")),
    ("unified_trader", "unified-paper.json", ("closed",)),
    ("adaptive_engine", "adaptive-state.json", ("closed_trades",)),
    ("regime_trader", "regime-state.json", ("log",)),
    ("futures_executor", "futures-trades.json", ("closed",)),
    ("live_executor", "live-trades.json", ("closed",)),
]


def _dig(d: dict, path: tuple):
    """Follow a key-path, returning [] when any level is missing/not a list."""
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return []
        cur = cur.get(k)
    return cur if isinstance(cur, list) else []


def tstat(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return 0.0 if sd == 0 else m / (sd / math.sqrt(n))


def _print_real_book(recs: list) -> None:
    """The trend REAL money book, in DOLLARS, UNKNOWN-tolerant (T0.10/T0.11).

    `pnl_usd` is already NET of the fill fee when present; it is null when the
    entry, exit or fee was unknown at close, so those records are UNKNOWN, never
    $0.00. This block never invents a per-symbol percent from the paper log — a
    real close with no priced PnL is shown as UNKNOWN and excluded.
    """
    import book_units                      # local import; scripts/ on sys.path
    known = [r for r in recs if isinstance(r.get("pnl_usd"), (int, float))]
    n_unknown = len(recs) - len(known)
    print("  TREND REAL MONEY BOOK  (trend-state.json -> real.closed)")
    if not recs:
        print("    no real closes recorded yet.")
        print()
        return
    if not known:
        print(f"    no real closes with a known PnL yet "
              f"({n_unknown} UNKNOWN — not counted).")
        print()
        return
    per_sym = defaultdict(list)
    for r in known:
        per_sym[r.get("symbol") or r.get("pair") or "?"].append(r["pnl_usd"])
    total = sum(r["pnl_usd"] for r in known)
    wins = sum(1 for r in known if r["pnl_usd"] > 0)
    note = f"  ({n_unknown} of {len(recs)} UNKNOWN — not counted)" if n_unknown else ""
    print(f"    {len(known)} priced closes | net {total:+.4f} USD | "
          f"win {wins / len(known) * 100:.0f}%{note}")
    for sym, usd in sorted(per_sym.items(), key=lambda kv: sum(kv[1])):
        print(f"      {sym:<12} n={len(usd):<3} net {sum(usd):+8.4f} USD")
    s = book_units.summarise_dollars("real_closed", recs)
    print(f"    (avg {s['avg_usd']:+.4f} USD/trade; "
          f"unknown carried as UNKNOWN, never averaged in as 0)")
    print()


def main():
    per_book = defaultdict(lambda: defaultdict(list))
    skipped = defaultdict(int)
    real_recs = []                      # the trend REAL money book (raw records)
    for book, fname, keypath in SOURCES:
        p = BASE / "data" / fname
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        recs = _dig(d, keypath)
        if book == "real_closed":
            real_recs = recs
        for rec in recs:
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

    # The REAL money book first, reported in DOLLARS and honest about UNKNOWN
    # (T0.10/T0.11). It must not be folded into the percent aggregation below:
    # those are paper/percent books, this one is real fills with a net pnl_usd.
    _print_real_book(real_recs)

    # aggregate across books, but keep the per-book split visible for the worst.
    # real_closed is EXCLUDED: it is the real money book, already reported in
    # dollars above. Mixing its trades into a percent verdict across paper books
    # answers neither question (T0.11).
    agg = defaultdict(list)
    for book, syms in per_book.items():
        if book == "real_closed":
            continue
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
