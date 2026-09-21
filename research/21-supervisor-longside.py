"""SUPERVISOR — is the LONG side structurally bad, or just in bad periods?

The live config: SHORT +1.084%/trade, LONG -0.385%/trade. The long half is
destroying ~29% of the total edge.

BUT: segment 1 (the bull period) had 61% WR. So the long side may be fine in
bull markets and terrible elsewhere. Deleting it outright would be a mistake
if it is period-dependent rather than structurally bad.

Decompose segment x side. This is the difference between "the long side is
broken" (remove it) and "the long side needs a filter" (fix it).
"""
import json
import statistics

FEE = 0.00144
HOLD = 21
SMA_TREND = 50
SMA_REGIME = 150
TP_R = 2.0
STOP_ATR = 2.5

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


def breadth_at(i):
    up = tot = 0
    for c in coins:
        s = sma(px[c], i, SMA_TREND)
        if s:
            tot += 1
            up += px[c][i] > s
    return up / tot if tot else 0.5


rows = []
for i in range(210, n - 2):
    s200 = sma(px["BTC"], i, SMA_REGIME)
    bear = bool(s200 and px["BTC"][i] < s200)
    btcm = px["BTC"][i] / px["BTC"][i - 30] - 1
    brd = breadth_at(i)
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
        risk = min(STOP_ATR * vol, 0.25)
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
            s = sma(a, j, SMA_TREND)
            if s and ((side == 1 and a[j] < s) or (side == -1 and a[j] > s)):
                r = move - FEE
                break
        if r is None:
            jx = min(i + HOLD, n - 1)
            r = (a[jx] / entry - 1) * side - FEE
        rows.append({"seg": min(2, i // SEG), "side": side, "ret": r,
                     "btcm": btcm, "brd": brd, "dist": a[i] / s50 - 1,
                     "vol": vol, "gold": c == "XAUT", "why": None,
                     "sym": c})

print("=" * 92)
print("1. SEGMENT x SIDE (is the long side broken, or period-dependent?)")
print("=" * 92)
labels = ["seg1 (bull)", "seg2 (chop/bear)", "seg3 (mixed)"]
print(f"{'':18} {'n':>6} {'WR':>7} {'exp/trade':>11}")
for s in (1, -1):
    name = "LONG" if s == 1 else "SHORT"
    for sg in (0, 1, 2):
        g = [r for r in rows if r["seg"] == sg and r["side"] == s]
        if not g:
            continue
        wr = sum(1 for r in g if r["ret"] > 0) / len(g) * 100
        ex = statistics.mean([r["ret"] for r in g]) * 100
        print(f"  {name:6} {labels[sg]:12} {len(g):>6} {wr:>6.1f}% {ex:>+10.3f}%")
    g = [r for r in rows if r["side"] == s]
    wr = sum(1 for r in g if r["ret"] > 0) / len(g) * 100
    ex = statistics.mean([r["ret"] for r in g]) * 100
    print(f"  {name:6} {'ALL':12} {len(g):>6} {wr:>6.1f}% {ex:>+10.3f}%")
    print()

print("=" * 92)
print("2. IF WE FILTER THE LONG SIDE, WHAT WORKS?")
print("=" * 92)
longs = [r for r in rows if r["side"] == 1]
short = [r for r in rows if r["side"] == -1]


def rep(name, g):
    if len(g) < 100:
        return
    wr = sum(1 for r in g if r["ret"] > 0) / len(g) * 100
    ex = statistics.mean([r["ret"] for r in g]) * 100
    segs = []
    for sg in (0, 1, 2):
        gs = [r for r in g if r["seg"] == sg]
        segs.append(round(statistics.mean([r["ret"] for r in gs]) * 100, 2)
                    if gs else 0)
    pos = sum(1 for x in segs if x > 0)
    print(f"  {name:34} n={len(g):>5} WR {wr:>5.1f}% exp {ex:>+7.3f}% "
          f"segs {segs} {pos}/3")


rep("ALL long", longs)
rep("long: btc_mom > 10%", [r for r in longs if r["btcm"] > 0.10])
rep("long: btc_mom > 5%", [r for r in longs if r["btcm"] > 0.05])
rep("long: breadth > 60%", [r for r in longs if r["brd"] > 0.60])
rep("long: breadth > 70%", [r for r in longs if r["brd"] > 0.70])
rep("long: dist_sma > 10%", [r for r in longs if r["dist"] > 0.10])
rep("long: dist_sma > 20%", [r for r in longs if r["dist"] > 0.20])
rep("long: vol < 5%", [r for r in longs if r["vol"] < 0.05])
rep("long: gold only", [r for r in longs if r["gold"]])
rep("long: btc_mom>10% & brd>60%", [r for r in longs
                                    if r["btcm"] > 0.10 and r["brd"] > 0.60])
rep("long: btc_mom>10% & vol<5%", [r for r in longs
                                   if r["btcm"] > 0.10 and r["vol"] < 0.05])
print()
print("  --- for reference ---")
rep("ALL short", short)
rep("short: btc_mom < -10%", [r for r in short if r["btcm"] < -0.10])
rep("short: btc_mom < 0", [r for r in short if r["btcm"] < 0])
rep("short: breadth < 40%", [r for r in short if r["brd"] < 0.40])
