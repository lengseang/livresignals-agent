"""STEALING THE ONE GOOD IDEA: ML for TRADE QUALITY, not price.

Why the RL/ML repos usually fail: they predict PRICE. Price is ~random at
daily resolution -> the model overfits -> it backtests beautifully and dies
live. That is exactly the trap we already caught (momentum +116% -> -80%).

The version that CAN work: predict whether OUR SETUP will win, given the
entry features. Signal-to-noise is far better because we only ask the
question at moments we already decided to trade.

We already proved this crudely - 2 hand-picked features lifted WR from
44.5% to 56.7%. So: does a proper regularised model with MORE features
(including the new external feeds) beat the hand-picked gate?

Honest validation: leave-one-fold-out CV. Small sample (164) -> report the
limits, do not pretend.
"""
import importlib.util
import json
import statistics
import sys

try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    HAVE_SK = True
except ImportError:
    HAVE_SK = False
print("sklearn:", "available" if HAVE_SK else "NOT installed")

spec = importlib.util.spec_from_file_location(
    "sel", "/opt/trading-agent/research/11-selectivity.py")
sel = importlib.util.module_from_spec(spec)
sys.modules["sel"] = sel
spec.loader.exec_module(sel)
allt = sel.allt
DAYNUM = sel.days

fng = {int(k): v for k, v in json.load(
    open("/opt/trading-agent/research/fng.json")).items()}

FEATS = ["mom30", "atr_pct", "dist_sma", "breadth", "btc_mom", "rank",
         "is_gold", "bear", "fng_norm", "gold_x_trend"]


def build(t):
    f = fng.get(DAYNUM[t["i"]])
    row = dict(t)
    row["fng_norm"] = (f - 50) / 50 if f is not None else 0.0
    # interaction: gold only makes sense when it is trending
    row["gold_x_trend"] = (1.0 if t["is_gold"] else 0.0) * t["dist_sma"]
    return row


data = [build(t) for t in allt]
print(f"trades: {len(data)} | features: {len(FEATS)}")

# ---- folds: train on 2 folds, test on the third (honest OOS) ----
fold_id = []
f_size = min(280, max(140, (sel.n - 240) // 3))
starts = []
s = 220
while s + f_size <= sel.n:
    starts.append(s)
    s += f_size - 40
for t in data:
    f = next((k for k, st in enumerate(starts) if st <= t["i"] < st + f_size),
             None)
    fold_id.append(f)
print(f"folds: {len(starts)} | trades per fold: "
      f"{[fold_id.count(k) for k in range(len(starts))]}")

X = [[t[k] if not isinstance(t[k], bool) else int(t[k]) for k in FEATS]
     for t in data]
y = [1 if t["won"] else 0 for t in data]


def simple_gate(t):
    """the current 2-feature hand-picked gate"""
    return t["btc_mom"] > 0.05 and t["breadth"] > 0.12


def rep(name, mask):
    grp = [t for t, m in zip(data, mask) if m]
    if not grp:
        print(f"{name:34} (no trades)")
        return
    wr = sum(1 for t in grp if t["won"]) / len(grp) * 100
    ex = statistics.mean([t["ret"] for t in grp]) * 100
    w = [t["ret"] for t in grp if t["won"]]
    l = [t["ret"] for t in grp if not t["won"]]
    pf = (statistics.mean(w) / abs(statistics.mean(l))) if w and l else 0
    print(f"{name:34} n={len(grp):>4}  WR {wr:>5.1f}%  payoff {pf:.2f}  "
          f"exp {ex:+.3f}%")


print()
print("=" * 78)
print("BASELINES")
print("=" * 78)
rep("take everything", [True] * len(data))
rep("hand-picked gate (btc_mom+breadth)", [simple_gate(t) for t in data])

if HAVE_SK:
    print()
    print("=" * 78)
    print("LEARNED MODEL — leave-one-fold-out (train on 2, test on 1)")
    print("=" * 78)
    preds = [None] * len(data)
    for k in range(len(starts)):
        tr = [i for i, f in enumerate(fold_id) if f is not None and f != k]
        te = [i for i, f in enumerate(fold_id) if f == k]
        if len(tr) < 20 or not te:
            continue
        sc = StandardScaler().fit([X[i] for i in tr])
        m = LogisticRegression(C=0.3, max_iter=2000,
                               class_weight="balanced")
        m.fit(sc.transform([X[i] for i in tr]), [y[i] for i in tr])
        p = m.predict_proba(sc.transform([X[i] for i in te]))[:, 1]
        for i, pi in zip(te, p):
            preds[i] = pi
    got = [p for p in preds if p is not None]
    if got:
        print(f"trades scored OOS: {len(got)}")
        for thr in (0.45, 0.50, 0.55, 0.60):
            mask = [(p is not None and p >= thr) for p in preds]
            rep(f"ML prob >= {thr:.2f}", mask)
        # feature importance (fit on all, for interpretation only)
        sc = StandardScaler().fit(X)
        m = LogisticRegression(C=0.3, max_iter=2000, class_weight="balanced")
        m.fit(sc.transform(X), y)
        print()
        print("feature weights (positive = helps win):")
        for k, w in sorted(zip(FEATS, m.coef_[0]), key=lambda x: -abs(x[1])):
            bar = "+" if w > 0 else "-"
            print(f"   {k:16} {w:+.3f}  {bar * min(20, int(abs(w)*12))}")
        print()
        print("CORRELATION CHECK vs hand-picked gate:")
        overlap = sum(1 for t, p in zip(data, preds)
                      if p is not None and (p >= 0.55) == simple_gate(t))
        tot = sum(1 for p in preds if p is not None)
        print(f"   ML>=0.55 agrees with hand gate on {overlap}/{tot} "
              f"trades ({overlap/tot*100:.0f}%)")
