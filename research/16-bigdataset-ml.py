"""EXPANDED TRAINING SET — every candidate setup, not just the 3 we took.

The 164-sample ML test was meaningless (16 samples per feature). The fix is
not a better model - it is MORE LABELLED SETUPS.

For EVERY day and EVERY asset we generate the setup IF it would have triggered,
then simulate the exact live exit logic (ATR stop, 2R TP, SMA50 break, max
hold) and label the outcome. That turns ~164 samples into thousands.

Crucially this includes setups we would normally SKIP - which is exactly what
a filter needs to learn from. Then walk-forward: train on early folds, test on
later ones. No leakage: features use only data up to entry.
"""
import json
import statistics

FEE = 0.00144
STOP_ATR = 2.5
TP_R = 2.0
HOLD = 21
SMA_TREND = 50
SMA_REGIME = 150

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
print(f"{n} days x {len(coins)} assets")


def sma(a, i, k):
    if i < k:
        return None
    w = a[i - k:i]
    return sum(w) / k


def atr_frac(a, i, k=14):
    if i < k + 1:
        return None
    trs = [abs(a[j] - a[j - 1]) for j in range(i - k, i)]
    return (sum(trs) / len(trs)) / a[i]


def breadth_at(i):
    up = tot = 0
    for c in coins:
        s = sma(px[c], i, SMA_TREND)
        if s:
            tot += 1
            up += px[c][i] > s
    return up / tot if tot else 0.5


# ---------- generate every labelled setup ----------
rows = []
for i in range(210, n - 2):
    s200 = sma(px["BTC"], i, SMA_REGIME)
    bear = bool(s200 and px["BTC"][i] < s200)
    btc_mom = px["BTC"][i] / px["BTC"][i - 30] - 1
    brd = breadth_at(i)
    for c in coins:
        a = px[c]
        s50 = sma(a, i, SMA_TREND)
        if not s50:
            continue
        # entry condition (mirrors live logic)
        if bear:
            if a[i] >= s50:
                continue
            side = -1
        else:
            if a[i] <= s50:
                continue
            side = 1
        vol = atr_frac(a, i) or 0.03
        risk = min(STOP_ATR * vol, 0.25)
        entry = a[i]
        stop = entry * (1 - risk) if side == 1 else entry * (1 + risk)
        tp = entry * (1 + TP_R * risk) if side == 1 else entry * (1 - TP_R * risk)
        # walk forward to the exit
        ret = None
        why = "max-hold"
        for j in range(i + 1, min(i + HOLD + 1, n)):
            move = (a[j] / entry - 1) * side
            if side == 1 and a[j] <= stop:
                ret = -risk - FEE
                why = "stop"
                break
            if side == -1 and a[j] >= stop:
                ret = -risk - FEE
                why = "stop"
                break
            if move >= TP_R * risk:
                ret = TP_R * risk - FEE
                why = "tp"
                break
            s50j = sma(a, j, SMA_TREND)
            if s50j and ((side == 1 and a[j] < s50j) or
                         (side == -1 and a[j] > s50j)):
                ret = move - FEE
                why = "trend"
                break
        if ret is None:
            jx = min(i + HOLD, n - 1)
            ret = (a[jx] / entry - 1) * side - FEE
        rows.append({
            "i": i, "day": days[i], "symbol": c, "side": side,
            "mom30": a[i] / a[i - 30] - 1,
            "atr_pct": vol,
            "dist_sma": a[i] / s50 - 1,
            "breadth": brd,
            "btc_mom": btc_mom,
            "bear": bear,
            "is_gold": c == "XAUT",
            "ret": ret,
            "won": ret > 0,
            "why": why,
        })

print(f"labelled setups: {len(rows)}")
wr = sum(1 for r in rows if r["won"]) / len(rows) * 100
print(f"baseline WR {wr:.1f}%  exp {statistics.mean([r['ret'] for r in rows])*100:+.3f}%")
print(f"exit mix: ", {w: sum(1 for r in rows if r['why'] == w)
                     for w in ('tp', 'stop', 'trend', 'max-hold')})
json.dump(rows, open("/opt/trading-agent/research/labelled-setups.json", "w"))

# ---------- walk-forward ML vs the 2-feature rule ----------
try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler
    HAVE = True
except ImportError:
    HAVE = False

FEATS = ["mom30", "atr_pct", "dist_sma", "breadth", "btc_mom", "bear",
         "is_gold", "side"]
X = [[int(r[f]) if isinstance(r[f], bool) else r[f] for f in FEATS] for r in rows]
y = [1 if r["won"] else 0 for r in rows]
dayidx = [r["day"] for r in rows]

# time-ordered 3-way split
order = sorted(range(len(rows)), key=lambda k: dayidx[k])
third = len(order) // 3
segs = [order[:third], order[third:2 * third], order[2 * third:]]


def report(name, idxs):
    g = [rows[k] for k in idxs]
    if not g:
        return
    w = sum(1 for r in g if r["won"]) / len(g) * 100
    e = statistics.mean([r["ret"] for r in g]) * 100
    print(f"  {name:38} n={len(g):>6}  WR {w:>5.1f}%  exp {e:+.3f}%")


print()
print("=" * 84)
print("BASELINES (all 3 time segments)")
print("=" * 84)
for k, s in enumerate(segs):
    report(f"segment {k+1} (all setups)", s)

print()
print("hand-picked rule: btc_mom>5% AND breadth>12%")
for k, s in enumerate(segs):
    g = [i for i in s if rows[i]["btc_mom"] > 0.05 and rows[i]["breadth"] > 0.12]
    report(f"  segment {k+1} (rule)", g)

if HAVE:
    print()
    print("=" * 84)
    print("ML — train on segment 1, test on segments 2 and 3")
    print("=" * 84)
    tr = segs[0]
    for name, mk in [("logistic reg", lambda: LogisticRegression(
                          C=0.5, max_iter=3000, class_weight="balanced")),
                     ("gradient boost", lambda: GradientBoostingClassifier(
                          n_estimators=120, max_depth=3, learning_rate=0.06,
                          subsample=0.8, random_state=7))]:
        sc = StandardScaler().fit([X[i] for i in tr])
        m = mk()
        m.fit(sc.transform([X[i] for i in tr]), [y[i] for i in tr])
        print(f"  {name}:")
        for k in (1, 2):
            te = segs[k]
            p = m.predict_proba(sc.transform([X[i] for i in te]))[:, 1]
            for thr in (0.5, 0.6, 0.7):
                sel = [te[j] for j in range(len(te)) if p[j] >= thr]
                report(f"    seg{k+1} prob>={thr}", sel)
        # feature importance
        if hasattr(m, "feature_importances_"):
            print("    importance:", {f: round(v, 3) for f, v in
                                      sorted(zip(FEATS, m.feature_importances_),
                                             key=lambda x: -x[1])[:5]})
        else:
            print("    weights:", {f: round(v, 3) for f, v in
                                   sorted(zip(FEATS, m.coef_[0]),
                                          key=lambda x: -abs(x[1]))[:5]})
