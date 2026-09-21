"""VERIFY THE NO-TP FINDING before touching anything.

The frontier says removing the take-profit raises expectancy 4.3x
(+0.437% -> +1.861%). That is exactly the size of claim I have over-claimed
on twice (hold=30, the A-grade gate). So it gets the full treatment FIRST:

  1. per-segment expectancy (all 3 regimes?)
  2. guard, relative to the champion
  3. drop-the-best-segment
  4. sensitivity - is 'no TP' a knife-edge at the edge of the grid?
  5. MONTE CARLO on trade ORDER with real position sizing, because the
     "no TP" profile has a 2.76 payoff and a 40% WR: it may be highly
     path-dependent and could blow the drawdown budget.
"""
import json
import random
import statistics

FEE = 0.00144
HOLD = 21
SMA_TREND = 50
SMA_REGIME = 150
STOP_ATR = 1.5

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


def vs(stop_mult, exit_mode, tp_r):
    """returns per-segment rets AND per-segment R-multiples (for MC)"""
    per = [[], [], []]
    for i in range(210, n - 2):
        seg = min(2, i // SEG)
        s200 = sma(px["BTC"], i, SMA_REGIME)
        bear = bool(s200 and px["BTC"][i] < s200)
        for c in coins:
            a = px[c]
            s50 = sma(a, i, SMA_TREND)
            if not s50:
                continue
            side = -1 if bear else 1
            if (side == 1 and a[i] <= s50) or (side == -1 and a[i] >= s50):
                continue
            vol = atr(a, i) or 0.03
            risk = min(stop_mult * vol, 0.25)
            entry = a[i]
            stop = entry * (1 - risk) if side == 1 else entry * (1 + risk)
            r = None
            for j in range(i + 1, min(i + HOLD + 1, n)):
                move = (a[j] / entry - 1) * side
                if (a[j] <= stop) if side == 1 else (a[j] >= stop):
                    r = -risk - FEE
                    break
                if tp_r < 90 and move >= tp_r * risk:
                    r = tp_r * risk - FEE
                    break
                if exit_mode == "mom5":
                    if j >= 6 and (a[j] / a[j - 5] - 1) * side < 0:
                        r = move - FEE
                        break
                else:
                    s = sma(a, j, SMA_TREND)
                    if s and ((side == 1 and a[j] < s) or
                              (side == -1 and a[j] > s)):
                        r = move - FEE
                        break
            if r is None:
                jx = min(i + HOLD, n - 1)
                r = (a[jx] / entry - 1) * side - FEE
            per[seg].append(r / risk)      # R-multiple
    return per


def stat(per):
    allr = [x for s in per for x in s]
    wr = sum(1 for r in allr if r > 0) / len(allr) * 100
    ex = statistics.mean(allr)
    seg_ex = [statistics.mean(s) if s else 0 for s in per]
    return {"n": len(allr), "wr": wr, "expR": ex, "segs": seg_ex,
            "pos": sum(1 for x in seg_ex if x > 0)}


print("=" * 96)
print("1. PER-SEGMENT (all 3 regimes profitable?)")
print("=" * 96)
for label, tp in [("CHAMPION 2R", 2.0), ("NO TP", 99.0), ("3R", 3.0)]:
    s = stat(vs(1.5, "mom5", tp))
    print(f"  {label:12} WR {s['wr']:>5.1f}%  exp {s['expR']:>+6.3f}R  "
          f"segs {[round(x,2) for x in s['segs']]}  {s['pos']}/3")

print()
print("=" * 96)
print("2. GUARD (relative to champion: champion is 2/3 segments)")
print("=" * 96)
s = stat(vs(1.5, "mom5", 99.0))
ok_sz = s["n"] >= 20 * 2
ok_fc = s["n"] >= 50 * 3
ok_pos = s["pos"] >= 2
print(f"  {'PASS' if ok_sz else 'FAIL':4}  sample size     "
      f"{s['n']} setups / 2 params")
print(f"  {'PASS' if ok_pos else 'FAIL':4}  fold consistency {s['pos']}/3 "
      f"vs champion 2/3")
print(f"  {'PASS' if ok_fc else 'FAIL':4}  per-fold        "
      f"{s['n']//3}/segment")
print(f"  => {'✅ ADMISSIBLE' if all([ok_sz, ok_fc, ok_pos]) else '⛔ BLOCKED'}")

print()
print("=" * 96)
print("3. DROP-THE-BEST-SEGMENT")
print("=" * 96)
for drop in (0, 1, 2):
    g = [x for k, seg in enumerate(vs(1.5, "mom5", 99.0)) if k != drop
         for x in seg]
    print(f"  without segment {drop+1}: exp {statistics.mean(g):+.3f}R  "
          f"WR {sum(1 for x in g if x > 0)/len(g)*100:.1f}%  n={len(g)}  "
          f"{'✅' if statistics.mean(g) > 0 else '❌'}")

print()
print("=" * 96)
print("4. SENSITIVITY — is 'no TP' a cliff edge?")
print("=" * 96)
for tp in (2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 99.0):
    g = stat(vs(1.5, "mom5", tp))
    tag = "no TP" if tp > 90 else f"{tp:g}R"
    print(f"  TP {tag:>5}: exp {g['expR']:>+6.3f}R  WR {g['wr']:>5.1f}%  "
          f"{g['pos']}/3 segs")

print()
print("=" * 96)
print("5. MONTE CARLO — the payoff is 2.76 with a 40% WR. Path risk?")
print("=" * 96)
for label, tp in [("CHAMPION 2R", 2.0), ("NO TP", 99.0)]:
    allr = [x for segs in vs(1.5, "mom5", tp) for x in segs]
    end, dds = [], []
    random.seed(11)
    for _ in range(3000):
        s = random.sample(allr, min(300, len(allr)))
        eq, peak, mdd = 0.0, 0.0, 0.0
        for r in s:
            eq += r * 0.01               # 1% risk per trade
            peak = max(peak, eq)
            mdd = min(mdd, eq - peak)
        end.append(eq)
        dds.append(mdd)
    end.sort()
    print(f"  {label:12} median {statistics.median(end)*100:>+7.2f}%  "
          f"p5 {end[int(len(end)*.05)]*100:>+7.2f}%  "
          f"p95 {end[int(len(end)*.95)]*100:>+7.2f}%  "
          f"median maxDD {statistics.median(dds)*100:>7.2f}%")
print()
print("  (300 trades at 1% risk each, bootstrapped 3000x)")
