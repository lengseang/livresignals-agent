"""SUPERVISOR FINAL TEST — the chop gate.

DIAGNOSIS: the edge is entirely regime-concentrated.
  seg1 bull  -> longs  +9.42%
  seg3 bear  -> shorts +1.90%
  seg2 CHOP  -> longs -1.37% AND shorts -8.63%   <- 40% of all trades

Every side-filter failed (all 1/3 segments) because they filter WHICH TRADES
but not WHICH PERIODS. The actual cause is trading in chop.

HYPOTHESIS: a trend-QUALITY gate (not a trend DIRECTION gate) stands aside in
chop. Test structural, non-fitted measures of 'is there a trend at all':

  A. |30d BTC return| - trend magnitude. Chop = near zero in BOTH directions.
  B. BTC distance from SMA150 - a bull grinding sideways sits NEAR the SMA.
  C. BTC own 21d volatility - chop often = low realised vol + no progress.
  D. efficiency ratio (net move / total path) - THE textbook chop measure.
     ER = |close_now - close_30ago| / sum(|daily moves|). ~1 = clean trend,
     ~0 = pure chop, going nowhere while flailing.

D is the principled one: it directly measures 'distance travelled vs effort'.
Nothing here is fitted to returns - these are descriptions of market state.
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
BTC = px["BTC"]


def sma(a, i, k):
    return sum(a[i - k:i]) / k if i >= k else None


def atr(a, i, k=14):
    if i < k + 1:
        return None
    return (sum(abs(a[j] - a[j - 1]) for j in range(i - k, i)) / k) / a[i]


def efficiency_ratio(i, k=30):
    if i < k + 1:
        return None
    net = abs(BTC[i] - BTC[i - k])
    path = sum(abs(BTC[j] - BTC[j - 1]) for j in range(i - k + 1, i + 1))
    return net / path if path else 0.0


def gen(i):
    """all trades for day i"""
    s200 = sma(BTC, i, SMA_REGIME)
    bear = bool(s200 and BTC[i] < s200)
    out = []
    for c in coins:
        a = px[c]
        s50 = sma(a, i, SMA_TREND)
        if not s50:
            continue
        side = -1 if bear else 1
        if (side == 1 and a[i] <= s50) or (side == -1 and a[i] >= s50):
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
        out.append((r, min(2, i // SEG)))
    return out


# precompute
DATA = {}
for i in range(210, n - 2):
    for r, seg in gen(i):
        pass
    DATA[i] = {
        "btcm": abs(BTC[i] / BTC[i - 30] - 1),
        "er30": efficiency_ratio(i, 30) or 0.0,
        "brd": sum(1 for c in coins
                   if (sma(px[c], i, SMA_TREND) or 9e9) < px[c][i])
        / len(coins),
        "dist": abs(BTC[i] / (sma(BTC, i, SMA_REGIME) or BTC[i]) - 1),
        "trades": gen(i),
    }

print("=" * 100)
print("CHOP GATE TEST — does standing aside in chop fix the instability?")
print("=" * 100)


def evaluate(gate_fn, label):
    per = [[], [], []]
    for i, m in DATA.items():
        if not gate_fn(m):
            continue
        for r, seg in m["trades"]:
            per[seg].append(r)
    allr = [x for s in per for x in s]
    if len(allr) < 200:
        print(f"  {label:34} too few trades ({len(allr)})")
        return None
    wr = sum(1 for r in allr if r > 0) / len(allr) * 100
    ex = statistics.mean(allr) * 100
    seg_ex = [round(statistics.mean(s) * 100, 2) if s else 0 for s in per]
    pos = sum(1 for e in seg_ex if e > 0)
    print(f"  {label:34} n={len(allr):>5} WR {wr:>5.1f}% exp {ex:>+7.3f}% "
          f"segs {seg_ex} {pos}/3")
    return {"n": len(allr), "wr": wr, "exp": ex, "segs": seg_ex, "pos": pos}


base = evaluate(lambda m: True, "NO GATE (current)")
print()
print("  --- A. trend magnitude |30d BTC return| ---")
for t in (0.03, 0.05, 0.08, 0.10, 0.15):
    evaluate(lambda m, t=t: m["btcm"] > t, f"  |btc_mom| > {t*100:.0f}%")
print()
print("  --- D. efficiency ratio (net move vs path travelled) ---")
for t in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30):
    evaluate(lambda m, t=t: m["er30"] > t, f"  ER(30) > {t:.2f}")
print()
print("  --- combined: ER + trend magnitude ---")
evaluate(lambda m: m["er30"] > 0.10 and m["btcm"] > 0.05,
         "  ER>0.10 AND |mom|>5%")
evaluate(lambda m: m["er30"] > 0.15 and m["btcm"] > 0.05,
         "  ER>0.15 AND |mom|>5%")
evaluate(lambda m: m["er30"] > 0.15 and m["btcm"] > 0.08,
         "  ER>0.15 AND |mom|>8%")
