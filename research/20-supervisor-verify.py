"""SUPERVISOR VERIFICATION — before promoting ANYTHING, run the guard.

I over-claimed twice (hold=30, the A-grade gate). Both times the fix was a
harder test AFTER the exciting result. Doing that up front this time.

Checks on the candidate atr1.5+mom5 vs live atr2.5+sma50:
  1. per-segment EXPECTANCY (not just WR) - are all 3 segments profitable?
  2. guard: sample size, fold consistency, per-fold size
  3. does the win survive if we DROP the best segment?
  4. parameter sensitivity: is atr1.5 a knife-edge, or a plateau?
  5. does it survive on the SHORT side alone, and LONG alone?
     (we know the alpha lives short - a long-side-only artefact would be junk)
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
d = {c: {int(k): v for k, v in dd.items()} for dd in [d] for k, v in dd.items()} \
    if False else {c: {int(k): v for k, v in dd.items()} for c, dd in d.items()}
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


def bt(mult, exit_mode, side_filter=None, drop_seg=None):
    """returns per-segment (list_of_returns, n)"""
    per = [[], [], []]
    for i in range(210, n - 2):
        seg = min(2, i // SEG)
        if drop_seg is not None and seg == drop_seg:
            continue
        s200 = sma(px["BTC"], i, SMA_REGIME)
        bear = bool(s200 and px["BTC"][i] < s200)
        for c in coins:
            a = px[c]
            s50 = sma(a, i, SMA_TREND)
            if not s50:
                continue
            side = -1 if bear else 1
            if side_filter and side != side_filter:
                continue
            if side == 1 and a[i] <= s50:
                continue
            if side == -1 and a[i] >= s50:
                continue
            vol = atr(a, i) or 0.03
            risk = min(mult * vol, 0.25)
            entry = a[i]
            stop = entry * (1 - risk) if side == 1 else entry * (1 + risk)
            r = None
            for j in range(i + 1, min(i + HOLD + 1, n)):
                move = (a[j] / entry - 1) * side
                if (a[j] <= stop) if side == 1 else (a[j] >= stop):
                    r = -risk - FEE
                    break
                if move >= TP_R * risk:
                    r = TP_R * risk - FEE
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
            per[seg].append(r)
    return per


def stats(per):
    allr = [x for s in per for x in s]
    if not allr:
        return None
    wr = sum(1 for r in allr if r > 0) / len(allr) * 100
    ex = statistics.mean(allr) * 100
    seg_ex = [statistics.mean(s) * 100 if s else 0 for s in per]
    seg_wr = [sum(1 for r in s if r > 0) / len(s) * 100 if s else 0
              for s in per]
    return {"n": len(allr), "wr": wr, "exp": ex, "seg_ex": seg_ex,
            "seg_wr": seg_wr}


print("=" * 96)
print("1. PER-SEGMENT EXPECTANCY (WR alone is not enough)")
print("=" * 96)
for label, mult, ex_mode in [("LIVE  atr2.5+sma50", 2.5, "sma50"),
                             ("CAND  atr1.5+mom5 ", 1.5, "mom5"),
                             ("CAND  atr2.0+mom5 ", 2.0, "mom5"),
                             ("CAND  atr1.5+sma50", 1.5, "sma50")]:
    s = stats(bt(mult, ex_mode))
    pos = sum(1 for e in s["seg_ex"] if e > 0)
    print(f"  {label}  WR {s['wr']:>5.1f}%  exp {s['exp']:>+7.3f}%")
    print(f"       segment WRs : {[round(w,1) for w in s['seg_wr']]}")
    print(f"       segment exps: {[round(e,3) for e in s['seg_ex']]}  "
          f"-> {pos}/3 positive")

print()
print("=" * 96)
print("2. GUARD (the same checks that caught hold=30)")
print("=" * 96)
s = stats(bt(1.5, "mom5"))
ok_n = s["n"] >= 20 * 2
ok_folds = 3 >= 3
pos = sum(1 for e in s["seg_ex"] if e > 0)
ok_cons = pos >= 3
ok_per = s["n"] >= 50 * 3
for name, ok, detail in [("sample size", ok_n, f"{s['n']} setups / 2 params"),
                         ("fold count", ok_folds, "3 segments"),
                         ("fold consistency", ok_cons,
                          f"{pos}/3 segments profitable (need 3)"),
                         ("per-fold", ok_per, f"{s['n']//3} setups/segment")]:
    print(f"  {'PASS' if ok else 'FAIL':4}  {name:18} {detail}")
print(f"  => {'✅ ADMISSIBLE' if all([ok_n, ok_folds, ok_cons, ok_per]) else '⛔ BLOCKED'}")

print()
print("=" * 96)
print("3. DROP-THE-BEST-SEGMENT TEST (does one lucky period carry it?)")
print("=" * 96)
base = stats(bt(1.5, "mom5"))
for drop in (0, 1, 2):
    s2 = stats(bt(1.5, "mom5", drop_seg=drop))
    print(f"  without segment {drop+1}: exp {s2['exp']:>+7.3f}%  "
          f"WR {s2['wr']:>5.1f}%  n={s2['n']}  "
          f"{'✅ still positive' if s2['exp'] > 0 else '❌ turns negative'}")

print()
print("=" * 96)
print("4. PARAMETER SENSITIVITY (knife-edge or plateau?)")
print("=" * 96)
print("  stop multiplier sweep (mom5 exit):")
for m in (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0):
    st = stats(bt(m, "mom5"))
    segp = sum(1 for e in st["seg_ex"] if e > 0)
    bar = "#" * max(1, int(st["exp"] * 40))
    print(f"    {m:>4}x ATR  exp {st['exp']:>+7.3f}%  WR {st['wr']:>5.1f}%  "
          f"{segp}/3 segs  {bar}")

print()
print("=" * 96)
print("5. SIDE BREAKDOWN (the alpha lives SHORT - a long-only artefact is junk)")
print("=" * 96)
for label, sf in [("SHORT only", -1), ("LONG only", 1)]:
    a = stats(bt(1.5, "mom5", side_filter=sf))
    b = stats(bt(2.5, "sma50", side_filter=sf))
    print(f"  {label}:")
    print(f"    CAND atr1.5+mom5 : exp {a['exp']:>+7.3f}%  WR {a['wr']:>5.1f}%  n={a['n']}")
    print(f"    LIVE atr2.5+sma50: exp {b['exp']:>+7.3f}%  WR {b['wr']:>5.1f}%  n={b['n']}")
