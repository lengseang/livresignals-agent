"""BUILD THE A-GRADE FILTER — stack the separators found in 11-selectivity.

Each separator is theoretically sound (not just a data-mined curiosity):
  BTC momentum   - don't fight the market backdrop
  breadth        - don't trade when the whole market is sick
  dist above SMA50 - only take STRONG trends, not marginal ones
  low volatility - clean trends beat choppy ones
  gold slot      - the only real diversifier

Score = how many A-grade conditions a setup satisfies. Then ask:
does requiring a higher score actually raise win rate, and what does it
cost in trade count? A filter that halves trades but lifts WR from 45%
to 60% is a BETTER product - fewer, higher-quality signals.
"""
import importlib.util
import statistics
import sys

spec = importlib.util.spec_from_file_location(
    "sel", "/opt/trading-agent/research/11-selectivity.py")
sel = importlib.util.module_from_spec(spec)
sys.modules["sel"] = sel
spec.loader.exec_module(sel)
allt = sel.allt

# thresholds taken from the bucket analysis
BTC_MOM_MIN = 0.05
BREADTH_MIN = 0.12
DIST_MIN = 0.05
VOL_MAX = 0.043


def score(t):
    s = 0
    s += t["btc_mom"] > BTC_MOM_MIN
    s += t["breadth"] > BREADTH_MIN
    s += t["dist_sma"] > DIST_MIN
    s += t["atr_pct"] < VOL_MAX
    return s


def stats(grp):
    if not grp:
        return None
    wr = sum(1 for t in grp if t["won"]) / len(grp) * 100
    ex = statistics.mean([t["ret"] for t in grp]) * 100
    w = [t["ret"] for t in grp if t["won"]]
    l = [t["ret"] for t in grp if not t["won"]]
    pf = (statistics.mean(w) / abs(statistics.mean(l))) if w and l else 0
    return len(grp), wr, pf, ex


print(f"total setups: {len(allt)}")
b = stats(allt)
print(f"BASELINE (take everything):  n={b[0]:>3}  WR {b[1]:.1f}%  "
      f"payoff {b[2]:.2f}  exp {b[3]:+.3f}%")
print()
print("=" * 78)
print(f"{'filter':34} {'n':>5} {'WR':>7} {'payoff':>8} {'exp/tr':>9} {'kept':>7}")
print("=" * 78)
for req in (1, 2, 3, 4):
    grp = [t for t in allt if score(t) >= req]
    s = stats(grp)
    if not s:
        continue
    kept = s[0] / len(allt) * 100
    print(f"{'score >= ' + str(req) + ' (A-grade)':34} {s[0]:>5} {s[1]:>6.1f}% "
          f"{s[2]:>8.2f} {s[3]:>8.3f}% {kept:>6.0f}%")

print()
print("--- individual filters (isolated) ---")
for name, fn in [
        (f"BTC mom > {BTC_MOM_MIN:.0%}", lambda t: t["btc_mom"] > BTC_MOM_MIN),
        (f"breadth > {BREADTH_MIN:.0%}", lambda t: t["breadth"] > BREADTH_MIN),
        (f"dist>SMA50 > {DIST_MIN:.0%}", lambda t: t["dist_sma"] > DIST_MIN),
        (f"vol < {VOL_MAX:.1%}", lambda t: t["atr_pct"] < VOL_MAX),
        ("gold only", lambda t: t["is_gold"]),
        ("crypto only", lambda t: not t["is_gold"]),
]:
    grp = [t for t in allt if fn(t)]
    s = stats(grp)
    if s:
        print(f"   {name:26} n={s[0]:>3}  WR {s[1]:>5.1f}%  payoff {s[2]:.2f}  "
              f"exp {s[3]:+.3f}%")

print()
print("--- best combination: gold slot + core filters ---")
for name, fn in [
        ("gold OR score>=2", lambda t: t["is_gold"] or score(t) >= 2),
        ("gold OR score>=3", lambda t: t["is_gold"] or score(t) >= 3),
        ("btc_mom>5% AND breadth>12%",
         lambda t: t["btc_mom"] > BTC_MOM_MIN and t["breadth"] > BREADTH_MIN),
        ("btc_mom>5% AND dist>5%",
         lambda t: t["btc_mom"] > BTC_MOM_MIN and t["dist_sma"] > DIST_MIN),
]:
    grp = [t for t in allt if fn(t)]
    s = stats(grp)
    if s:
        print(f"   {name:30} n={s[0]:>3}  WR {s[1]:>5.1f}%  payoff {s[2]:.2f}  "
              f"exp {s[3]:+.3f}%  kept {s[0]/len(allt)*100:.0f}%")
