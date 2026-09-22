"""test_book_units_against_writers.py — the test the first suite was missing.

WHY THIS EXISTS
The first suite passed while `futures_executor` was declared a 'fraction' when it
is 'dollars'. The suite could not catch that, because every check it made was
INTERNAL: it asserted the summariser agreed with itself. A self-consistent wrong
premise passes any amount of self-consistency checking.

This file instead checks each book's declared unit against an INDEPENDENT fact
that the declaration cannot influence:

  1. the arithmetic the writer itself performs, re-run here from raw fields
  2. the identity that must hold for the unit to be right

If the declaration is wrong, one of these disagrees.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/opt/trading-agent/scripts")
from book_units import BOOKS, as_percent  # noqa: E402

D = Path("/opt/trading-agent/data")
load = lambda n: json.loads((D / n).read_text())

fails = []


def check(label, cond, detail=""):
    print(f"  {'✅' if cond else '🔴'} {label}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(label)


print("=" * 78)
print("A. RE-DERIVE EACH BOOK'S UNIT FROM ITS OWN WRITER EXPRESSION")
print("=" * 78)

fut = load("futures-trades.json")["closed"]
print("\n  futures_executor — writer is `pnl = (px - entry) * qty`")
for t in fut[:3]:
    repro = (t["exit"] - t["entry"]) * t["qty"]
    check(f"{t['reason']} (exit-entry)*qty == stored pnl",
          abs(repro - t["pnl"]) < 1e-3,
          f"recomputed {repro:+.4f} vs stored {t['pnl']:+.4f}")
    # If pnl were a FRACTION, |pnl| would be ~0.01. It is ~0.09 and equals
    # (price move * qty), so it is DOLLARS. Assert that explicitly.
    frac_would_be = (t["exit"] - t["entry"]) / t["entry"]
    check(f"  pnl is NOT the return fraction (frac would be {frac_would_be:+.5f})",
          abs(t["pnl"] - frac_would_be) > 1e-4,
          f"stored {t['pnl']:+.5f} differs -> it is dollars")

ad = load("adaptive-state.json")["closed_trades"]
print("\n  adaptive_engine — writer is `pnl = (px-entry)/entry * size`, size=50")
for t in ad[:3]:
    r = (t["exit"] - t["entry"]) / t["entry"]
    # Tolerance note: `pnl` is rounded to 4dp and the entry/exit prices are
    # rounded to 2-4dp, so the recovered ratio carries that error. 50.0149 vs a
    # true 50.0 is rounding, not a wrong size — asserting exactness here would be
    # a test that fails on correct data, which is the failure mode this whole
    # exercise has been about.
    check(f"{t['symbol']} pnl / r == 50 within rounding (confirms $50 size)",
          abs(t["pnl"] / r - 50.0) < 0.1,
          f"{t['pnl']:+.4f} / {r:+.6f} = {t['pnl']/r:.4f}")

un = load("unified-paper.json")["closed"]
print("\n  unified_trader — writer is `pnl = (px-entry) * (size/entry)`, size=5")
for t in un[:3]:
    if not all(isinstance(t.get(k), (int, float)) for k in ("entry", "exit")):
        continue
    repro = (t["exit"] - t["entry"]) * (t["size"] / t["entry"])
    check(f"{t['symbol']} (exit-entry)*(size/entry) == pnl",
          abs(repro - t["pnl"]) < 1e-3,
          f"recomputed {repro:+.4f} vs stored {t['pnl']:+.4f}")

print()
print("=" * 78)
print("B. THE DECLARATION AND THE DATA MUST AGREE")
print("=" * 78)
print("\n  For every book, as_percent() must reproduce the price-derived return")
print("  when prices exist. This is the check the first suite lacked: it compares")
print("  the declaration against something OUTSIDE the declaration.")

CASES = [
    ("futures_executor", "futures-trades.json", "closed"),
    ("adaptive_engine", "adaptive-state.json", "closed_trades"),
    ("unified_trader", "unified-paper.json", "closed"),
    ("regime_trader", "regime-state.json", "log"),
]
for book, fname, key in CASES:
    recs = load(fname)[key]
    mism = []
    for t in recs:
        e, x = t.get("entry"), t.get("exit")
        if not (isinstance(e, (int, float)) and isinstance(x, (int, float)) and e):
            continue
        want = (x - e) / e * 100
        got = as_percent(book, t)
        # Publishers round their stored return (regime_trader: round(move, 5);
        # futures: prices at 2-6dp). Comparing at 1e-6 would flag every record
        # as a mismatch purely because of that rounding — a false positive on
        # entirely correct data. Tolerance is set just above the stored
        # resolution, so a genuine unit error (which is off by a factor, not by
        # 1e-4) is still caught.
        if got is None or abs(got - want) > 5e-3:
            mism.append((t.get("symbol"), want, got))
    if mism:
        for sym, w, g in mism[:3]:
            print(f"      {sym}: prices say {w:+.4f}% but declaration says "
                  f"{g if g is None else f'{g:+.4f}%'}")
    check(f"{book}: {len(recs)} records, declaration matches prices",
          not mism, f"{len(mism)} mismatches")

print()
print("=" * 78)
print("C. THE THREE HISTORICAL FABRICATIONS ARE ALL GONE")
print("=" * 78)
from book_units import summarise  # noqa: E402

for book, fname, key, was in (
        ("adaptive_engine", "adaptive-state.json", "closed_trades", "-251% DD"),
        ("unified_trader", "unified-paper.json", "closed", "+1589%/-399% DD"),
        ("futures_executor", "futures-trades.json", "closed", "-10.51%/-20.12%")):
    s = summarise(book, load(fname)[key])
    check(f"{book}: sane now (was {was})",
          -100 <= s["max_drawdown"] <= 0 and abs(s["cumulative"]) < 5000,
          f"cum {s['cumulative']:+.2f}%  maxDD {s['max_drawdown']:.2f}%")

print("\n  futures, against the independently computed truth:")
fut_rets = [(t["exit"] / t["entry"] - 1) * 100 for t in fut]
fut_margin = [r * 3 for r in fut_rets]
c = pk = dd = 0.0
for r in fut_margin:
    c += r
    pk = max(pk, c)
    dd = min(dd, c - pk)
check("futures cumulative == -3.93% on margin (not the -10.51% first reported)",
      abs(c + 3.93) < 0.05, f"got {c:+.2f}%")
check("futures maxDD == -7.77% on margin (not the -20.12% first reported)",
      abs(dd + 7.77) < 0.05, f"got {dd:.2f}%")

print()
print("=" * 78)
print(f"RESULT: {'✅ all checks pass' if not fails else f'🔴 {len(fails)} FAILURES'}")
for f in fails:
    print(f"  🔴 {f}")
print("=" * 78)
sys.exit(1 if fails else 0)
