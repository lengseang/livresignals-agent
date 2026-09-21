"""SUPERVISOR DECISION ANALYSIS — capital-normalised, stability-weighted.

THE TRAP IN THE PREVIOUS RUN:
  A wider stop wins more often (39% -> 1.3% stop-outs, WR 38->42%) BUT under
  risk-parity sizing a wider stop means a SMALLER POSITION. So a +1.16%
  per-position return at a 35% stop is NOT comparable to +0.42% at a 10%
  stop. The honest metric is return PER UNIT OF RISK:

      capital_expectancy = per_position_expectancy / stop_distance

This is the seesaw again in a new costume: you can win more often, or make
more per unit of risk, but not both. A supervisor must pick the right one
deliberately instead of being seduced by the win rate.

WHAT WE SCORE (in order):
  1. STABILITY  - max WR spread across the 3 time segments (the real disease)
  2. RISK-ADJ    - capital expectancy per unit of risk
  3. WR         - what the product shows subscribers
  4. edge/stop  - a crude Ruin-distance check: how far a losing streak costs
"""
import json
import math
import statistics

FEE = 0.00144
HOLD = 21
SMA_TREND = 50
SMA_REGIME = 150
TP_R = 2.0

gold = {int(k): float(v) for k, v in json.load(
    open("/opt/trading-agent/research/price-cache-gold-5y.json")).items()}
d = json.load(open("/opt/trading-agent/research/price-cache-1000d.json"))
d = {c: {int(k): v for k, v in dd.items()} for c, dd in d.items()}
d.pop("MATIC", None)
d["XAUT"] = gold
days = sorted(set.intersection(*[set(x) for x in d.values()]))
px = {c: [d[c][t] for t in days] for c in d}
coins = sorted(d)
n = len(days)
SEG = n // 3


def sma(a, i, k):
    return sum(a[i - k:i]) / k if i >= k else None


def atr(a, i, k=14):
    if i < k + 1:
        return None
    return (sum(abs(a[j] - a[j - 1]) for j in range(i - k, i)) / k) / a[i]


def risk_of(mode, a, i, vol):
    if mode == "atr2.5":                     # CURRENT LIVE
        return min(2.5 * vol, 0.25)
    if mode == "atr1.5":
        return min(1.5 * vol, 0.20)
    if mode == "atr2.0":
        return min(2.0 * vol, 0.25)
    if mode == "atr3.5":
        return min(3.5 * vol, 0.30)
    if mode == "sqrt2.5":
        return min(2.5 * vol * math.sqrt(HOLD), 0.35)
    if mode == "vol21x2":
        if i < 22:
            return None
        rets = [a[j] / a[j - 1] - 1 for j in range(i - 21, i)]
        return min(2.0 * statistics.pstdev(rets), 0.35)
    if mode == "vol21x2.5":
        if i < 22:
            return None
        rets = [a[j] / a[j - 1] - 1 for j in range(i - 21, i)]
        return min(2.5 * statistics.pstdev(rets), 0.40)
    return None


def trade(a, i, side, risk, exit_mode):
    entry = a[i]
    stop = entry * (1 - risk) if side == 1 else entry * (1 + risk)
    for j in range(i + 1, min(i + HOLD + 1, n)):
        move = (a[j] / entry - 1) * side
        if (a[j] <= stop) if side == 1 else (a[j] >= stop):
            return -risk - FEE, "stop"
        if move >= TP_R * risk:
            return TP_R * risk - FEE, "tp"
        if exit_mode == "sma50":
            s = sma(a, j, SMA_TREND)
            if s and ((side == 1 and a[j] < s) or (side == -1 and a[j] > s)):
                return move - FEE, "trend"
        elif exit_mode == "mom5":
            if j >= 6 and (a[j] / a[j - 5] - 1) * side < 0:
                return move - FEE, "mom"
    jx = min(i + HOLD, n - 1)
    return (a[jx] / entry - 1) * side - FEE, "hold"


MODES = ["atr1.5", "atr2.0", "atr2.5", "atr3.5", "sqrt2.5", "vol21x2",
         "vol21x2.5"]
EXITS = ["sma50", "mom5"]

print("=" * 108)
print("CAPITAL-NORMALISED, STABILITY-WEIGHTED COMPARISON")
print("=" * 108)
print(f"{'config':22} {'n':>6} {'WR':>6} {'exp/pos':>9} {'risk':>6} "
      f"{'exp/risk':>9} {'spread':>7} {'segs':>22}")
print("-" * 108)

table = []
for mode in MODES:
    for ex in EXITS:
        per = [[], [], []]
        risks = []
        for i in range(210, n - 2):
            s200 = sma(px["BTC"], i, SMA_REGIME)
            bear = bool(s200 and px["BTC"][i] < s200)
            for c in coins:
                a = px[c]
                s50 = sma(a, i, SMA_TREND)
                if not s50:
                    continue
                side = -1 if bear else 1
                if side == 1 and a[i] <= s50:
                    continue
                if side == -1 and a[i] >= s50:
                    continue
                vol = atr(a, i) or 0.03
                rk = risk_of(mode, a, i, vol)
                if rk is None:
                    continue
                r, _ = trade(a, i, side, rk, ex)
                per[min(2, i // SEG)].append(r)
                risks.append(rk)
        allr = [x for s in per for x in s]
        if not allr:
            continue
        wrs = [sum(1 for r in s if r > 0) / len(s) * 100 for s in per if s]
        wr = sum(1 for r in allr if r > 0) / len(allr) * 100
        exp = statistics.mean(allr) * 100
        rk = statistics.mean(risks) * 100
        spread = max(wrs) - min(wrs)
        table.append({"cfg": f"{mode}+{ex}", "n": len(allr), "wr": wr,
                      "exp": exp, "risk": rk, "ratio": exp / rk if rk else 0,
                      "spread": spread, "segs": wrs})
        print(f"{mode + '+' + ex:22} {len(allr):>6} {wr:>5.1f}% {exp:>+8.3f}% "
              f"{rk:>5.1f}% {exp/rk if rk else 0:>9.4f} {spread:>7.1f} "
              f"{wrs[0]:>6.1f}/{wrs[1]:>6.1f}/{wrs[2]:>6.1f}")

print()
print("=" * 108)
print("RANKINGS (each criterion stated explicitly, so the choice is auditable)")
print("=" * 108)
for key, label, rev in [("ratio", "RISK-ADJUSTED expectancy (exp per unit risk)", True),
                        ("spread", "STABILITY (smaller WR spread = better)", False),
                        ("wr", "WIN RATE (what subscribers see)", True)]:
    s = sorted(table, key=lambda x: x[key], reverse=rev)[:4]
    print(f"\n  by {label}:")
    for r in s:
        print(f"    {r['cfg']:22} ratio {r['ratio']:.4f}  spread {r['spread']:>4.1f}  "
              f"WR {r['wr']:.1f}%  n={r['n']}")

live = next(r for r in table if r["cfg"] == "atr2.5+sma50")
print()
print("=" * 108)
print(f"CURRENTLY LIVE: atr2.5+sma50  -> WR {live['wr']:.1f}%, ratio "
      f"{live['ratio']:.4f}, spread {live['spread']:.1f}")
print("=" * 108)
better = [r for r in table if r["ratio"] > live["ratio"] * 1.10
          and r["spread"] < live["spread"]]
if better:
    print("  candidates that beat the live config on BOTH risk-adjusted")
    print("  expectancy (>=10%) AND stability:")
    for r in sorted(better, key=lambda x: -x["ratio"]):
        print(f"    ✅ {r['cfg']:22} ratio {r['ratio']:.4f} "
              f"(+{(r['ratio']/live['ratio']-1)*100:.0f}%)  "
              f"spread {r['spread']:.1f} (vs {live['spread']:.1f})  "
              f"WR {r['wr']:.1f}%")
else:
    print("  ❌ NO config beats live on both. Keep the current config.")

json.dump(table, open("/tmp/supervisor-decision.json", "w"), indent=1)
