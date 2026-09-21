"""MACRO-DRIVEN GOLD ANALYSIS — does the 'flow/fuel' actually predict?

Owner's insight: technicals alone are incomplete; the real drivers are macro
flows (rates, dollar, demand). Test on 10 years of real data:
  - gold: GC=F daily closes
  - real yield: FRED DFII10 (10y TIPS) -- THE classic gold driver
  - dollar: FRED DTWEXBGS (trade-weighted USD)

Q1: does gold correlate with real yields / dollar?
Q2: does a macro-filtered strategy beat pure technical? (OOS validated)
"""
import csv
import json
from datetime import datetime

GOLD = "/tmp/yh_GC_F.json"
RY = "/tmp/fred_DFII10.csv"
DX = "/tmp/fred_DTWEXBGS.csv"


def load_gold():
    d = json.load(open(GOLD))
    r = d["chart"]["result"][0]
    out = {}
    for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]):
        if c:
            out[datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")] = float(c)
    return out


def load_fred(path):
    out = {}
    with open(path) as f:
        for row in csv.reader(f):
            if len(row) == 2 and row[1] not in (".", "", None):
                try:
                    out[row[0]] = float(row[1])
                except ValueError:
                    pass
    return out


gold = load_gold()
ry = load_fred(RY)
dx = load_fred(DX)
print(f"gold {len(gold)}d | real-yield {len(ry)}d | dollar {len(dx)}d")

# align on dates where all three exist
dates = sorted(set(gold) & set(ry) & set(dx))
print(f"aligned: {len(dates)} days  ({dates[0]} -> {dates[-1]})\n")
g = [gold[d] for d in dates]
y = [ry[d] for d in dates]
x = [dx[d] for d in dates]


def ret(v, i, n=1):
    return v[i] / v[i - n] - 1 if i >= n else 0.0


def corr(a, b):
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((p - ma) * (q - mb) for p, q in zip(a, b)) / n
    sa = (sum((p - ma) ** 2 for p in a) / n) ** 0.5
    sb = (sum((q - mb) ** 2 for q in b) / n) ** 0.5
    return cov / (sa * sb) if sa and sb else 0.0


# ---- Q1: correlations ------------------------------------------------
gr = [ret(g, i) for i in range(1, len(g))]
dy = [y[i] - y[i - 1] for i in range(1, len(y))]     # change in real yield
dxr = [ret(x, i) for i in range(1, len(x))]          # dollar returns

print("=== Q1: what drives gold? (daily, 10y) ===")
print(f"  corr(gold ret, change in real yield) = {corr(gr, dy):+.3f}")
print(f"  corr(gold ret, dollar ret)           = {corr(gr, dxr):+.3f}")

# 20-day horizon (less noise)
gr20 = [ret(g, i, 20) for i in range(20, len(g))]
dy20 = [y[i] - y[i - 20] for i in range(20, len(y))]
dx20 = [ret(x, i, 20) for i in range(20, len(x))]
print(f"  20d: corr(gold, d-real-yield) = {corr(gr20, dy20):+.3f}  "
      f"corr(gold, dollar) = {corr(gr20, dx20):+.3f}")

# ---- Q2: strategy comparison ----------------------------------------
def backtest(dates, start_i, end_i, use_trend, use_macro, name):
    """Long gold when conditions met; measure return over the window."""
    eq = 1.0
    inpos = False
    entry = 0.0
    trades = 0
    for i in range(max(start_i, 250), end_i):
        sma50 = sum(g[i - 50:i]) / 50 if i >= 50 else g[i]
        sma200 = sum(g[i - 200:i]) / 200 if i >= 200 else g[i]
        trend_ok = g[i] > sma50 and g[i] > sma200
        macro_ok = (y[i] < y[i - 20]) and (x[i] < x[i - 20])   # yields & USD falling
        if use_trend and use_macro:
            want = trend_ok and macro_ok
        elif use_trend:
            want = trend_ok
        elif use_macro:
            want = macro_ok
        else:
            want = True
        if want and not inpos:
            inpos, entry, trades = True, i, trades + 1
        elif not want and inpos:
            eq *= g[i] / g[entry]
            inpos = False
    if inpos:
        eq *= g[end_i - 1] / g[entry]
    return (eq - 1) * 100, trades


n = len(g)
mid = n // 2
rows = []
for use_trend, use_macro, name in [
        (False, False, "buy & hold"),
        (True, False, "technical only (SMA50+200)"),
        (False, True, "macro only (yields+USD falling)"),
        (True, True, "technical + MACRO")]:
    is_r, is_t = backtest(dates, 250, mid, use_trend, use_macro, name)
    oos_r, oos_t = backtest(dates, mid, n, use_trend, use_macro, name)
    rows.append((name, is_r, oos_r, is_t, oos_t))

print("\n=== Q2: strategy comparison (in-sample | out-of-sample) ===")
print(f"{'strategy':36} {'IS ret':>9} {'OOS ret':>9} {'tr':>5}")
for name, is_r, oos_r, is_t, oos_t in rows:
    flag = "✅" if oos_r > 0 else "  "
    print(f"{name:36} {is_r:>8.1f}% {oos_r:>8.1f}% {oos_t:>5} {flag}")
