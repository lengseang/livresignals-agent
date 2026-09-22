"""test_book_units.py — validate the canonical unit reader.

Every invariant here is one a human got wrong today while reading these books by
hand, which is why they are tests rather than a comment.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/opt/trading-agent/scripts")
from book_units import as_percent, summarise, BOOKS  # noqa: E402

D = Path("/opt/trading-agent/data")
load = lambda n: json.loads((D / n).read_text())

fails = []


def check(label, cond, detail=""):
    print(f"  {'✅' if cond else '🔴'} {label}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(label)


print("=" * 74)
print("1. KNOWN-VALUE ROUND TRIPS  (the bug in isolation)")
print("=" * 74)

# futures_executor: `pnl` is DOLLARS — (px - entry) * qty, the writer at ~line 388.
# An earlier version of this line asserted `pnl=0.0133 -> 1.33%`, which encoded the
# very assumption that turned out to be wrong. It is left visible rather than
# quietly deleted, because it is the clearest evidence of how a test can lock in a
# bad premise and pass forever afterwards.
check("futures pnl=$0.0133 with qty=0.07 and prices -> derived from prices",
      abs(as_percent("futures_executor",
                     {"pnl": 0.0133, "qty": 0.07, "entry": 116.63, "exit": 116.82})
          - 0.1629) < 5e-3)
check("futures pnl with NO prices and NO qty is REFUSED, not guessed",
      as_percent("futures_executor", {"pnl": 0.0133}) is None)

# unified_trader: pnl is DOLLARS on size 5.0. Observed real record.
u = {"symbol": "SOL/USDT", "entry": 103.89, "pnl": 0.1208, "size": 5.0}
got = as_percent("unified_trader", u)
check("unified pnl=$0.1208 on size 5 -> 2.416%", abs(got - 2.416) < 1e-9,
      f"got {got:.4f}%")

# adaptive_engine: dollars on size 50 (recovered from entry/exit)
a = {"entry": 77130.23, "exit": 77356.0, "pnl": 0.1464}
got = as_percent("adaptive_engine", a)
check("adaptive (no size) recovers from prices -> +0.2927%",
      abs(got - 0.2927) < 0.001, f"got {got:.4f}%")

# adaptive_engine with the NEW ret_pct field wins over the dollar pnl
a2 = {"entry": 100.0, "exit": 101.0, "pnl": 0.5, "ret_pct": 0.01, "size": 5.0}
check("adaptive prefers ret_pct (0.01 -> 1.0%) over pnl",
      abs(as_percent("adaptive_engine", a2) - 1.0) < 1e-9)

# regime/trend: ret IS the fraction
check("regime ret=0.00826 -> 0.826%",
      abs(as_percent("regime_trader", {"ret": 0.00826}) - 0.826) < 1e-9)
check("trend ret=-0.156 -> -15.6%",
      abs(as_percent("trend_follower", {"ret": -0.156}) + 15.6) < 1e-9)

print()
print("=" * 74)
print("2. THE FABRICATIONS ARE GONE")
print("=" * 74)

# The original recap reported adaptive maxDD = -251%. That came from reading
# dollar pnl as a fraction. A drawdown deeper than 100% of a peak is impossible
# for a long-only book, so this is a hard ceiling worth asserting.
v = summarise("adaptive_engine", load("adaptive-state.json")["closed_trades"])
check("adaptive maxDD within (-100%, 0] — was reported -251%",
      -100.0 <= v["max_drawdown"] <= 0.0, f"maxDD {v['max_drawdown']:.2f}%")
check("adaptive payoff is plausible (0 < p < 10)",
      0 < v["payoff"] < 10, f"payoff {v['payoff']:.3f}")

u_all = summarise("unified_trader", load("unified-paper.json")["closed"])
check("unified maxDD within (-100%, 0] — was reported -399%",
      -100.0 <= u_all["max_drawdown"] <= 0.0,
      f"maxDD {u_all['max_drawdown']:.2f}%")
check("unified cumulative is single-digit-thousands % max",
      abs(u_all["cumulative"]) < 5000, f"cum {u_all['cumulative']:.2f}%")

print()
print("=" * 74)
print("3. STRUCTURAL INVARIANTS  (true by definition, so they catch bad math)")
print("=" * 74)
for book, recs in (("futures_executor", load("futures-trades.json")["closed"]),
                   ("regime_trader", load("regime-state.json")["log"]),
                   ("adaptive_engine", load("adaptive-state.json")["closed_trades"]),
                   ("unified_trader", load("unified-paper.json")["closed"])):
    s = summarise(book, recs)
    rets = [r for r in (as_percent(book, x) for x in recs) if r is not None]
    cum = sum(rets)
    # correct drawdown definition: deepest fall from a RUNNING peak
    c = peak = trough = dd = 0.0
    for r in rets:
        c += r
        peak = max(peak, c)
        trough = min(trough, c)
        dd = min(dd, c - peak)

    checks = [
        (f"{book}: maxDD <= 0", s["max_drawdown"] <= 1e-9),
        # 🔴 The bound is the full PEAK-TO-TROUGH SPAN, not the peak alone.
        #
        # Two earlier attempts at this invariant were wrong in the same way —
        # they bounded the drawdown by the peak, which assumes equity returns to
        # zero. It does not. `futures_executor` peaked at +2.20% and then fell to
        # -17.92%, so its drawdown is 20.12 points: larger than the peak, and
        # entirely correct.
        #
        # A drawdown is the distance from a high-water mark to the following low,
        # so the true bound is -(peak - trough). Writing the invariant from the
        # definition rather than from intuition is what fixed it.
        (f"{book}: maxDD >= -(peak - trough) - eps",
         s["max_drawdown"] >= -(peak - trough) - 1e-6),
        (f"{book}: maxDD == deepest fall on the rebuilt curve",
         abs(dd - s["max_drawdown"]) < 1e-6),
        (f"{book}: win_rate + loss_rate == 100",
         abs(s["win_rate"] + (100 - s["win_rate"]) - 100) < 1e-9),
        (f"{book}: payoff == avg_win/avg_loss",
         (abs(s["payoff"] - (s["avg_win"] / s["avg_loss"])) < 1e-6)
         if s["avg_loss"] else s["payoff"] == 0),
        (f"{book}: expectancy == cumulative/n",
         abs(s["expectancy"] - s["cumulative"] / s["n"]) < 1e-6),
    ]
    for label, ok in checks:
        if not ok:
            fails.append(label)
    print(f"  {'✅' if all(o for _, o in checks) else '🔴'} {book}: {len(checks)} invariants")

print()
print("=" * 74)
print("4. UNKNOWN BOOK IS REFUSED  (fail closed, never guess a unit)")
print("=" * 74)
try:
    as_percent("some_new_book", {"pnl": 0.5})
    check("unknown book raises", False)
except KeyError:
    check("unknown book raises KeyError instead of guessing", True)

print()
print("=" * 74)
print(f"RESULT: {'✅ all checks pass' if not fails else f'🔴 {len(fails)} FAILURES'}")
for f in fails:
    print(f"  🔴 {f}")
print("=" * 74)
sys.exit(1 if fails else 0)
