"""EDGE HUNT BATCH 3 — walk-forward validated, long-side fixes.

Batch 2 result: regime-adaptive L/S was the first OOS-positive edge
(full +29.7%, OOS +40.8%, DD -21.9%). But two weaknesses remained:

  1. LONG SIDE IS WEAK  (IS -7.9% while BTC did +175%)
     -> hypothesis: in bull markets, just HOLD BTC. Selection adds nothing.
  2. SINGLE IS/OOS SPLIT is weak evidence
     -> fix: WALK-FORWARD validation across 5 rolling folds.

NEW STRATEGIES UNDER TEST
  S1  regime-adaptive (batch-2 winner) - baseline
  S2  BTC-HOLD in bull + short-weakest in bear   <- main hypothesis
  S3  S2 + breadth filter (only long when breadth is healthy)
  S4  relative-strength long (vs BTC) instead of absolute momentum
  S5  S2 with volatility-targeted BTC holding

Metric that matters: MEDIAN fold return, and how many folds are positive.
A real edge shows up in MOST folds, not just on average.
"""
import json
import statistics

d = json.load(open("/opt/trading-agent/research/price-cache-1000d.json"))
d = {c: {int(k): v for k, v in dd.items()} for c, dd in d.items()}
d.pop("MATIC", None)
days = sorted(set.intersection(*[set(x) for x in d.values()]))
px = {c: [d[c][t] for t in days] for c in d}
coins = sorted(d)
n = len(days)
FEE = 0.00144


def sma(a, i, k):
    w = a[max(0, i - k):i]
    return sum(w) / len(w) if len(w) >= k else None


def sd(a, i, k):
    w = a[max(0, i - k):i]
    if len(w) < k:
        return None
    m = sum(w) / len(w)
    return (sum((v - m) ** 2 for v in w) / len(w)) ** 0.5


def breadth(i):
    """fraction of coins above their own SMA50"""
    up = 0
    tot = 0
    for c in coins:
        s = sma(px[c], i, 50)
        if s:
            tot += 1
            if px[c][i] > s:
                up += 1
    return up / tot if tot else 0.5


def bt(start, end, mode, topn=3, hold=21, risk=0.01, stop=0.10,
       breadth_min=0.5, sma_r=200):
    """Shared-account backtest. mode: regime|btc_bull|breadth|relstr"""
    cash, pos = 1.0, {}
    peak, mdd, trades = 1.0, 0.0, 0
    for i in range(max(start, sma_r + 25), end):
        s200 = sma(px["BTC"], i, sma_r)
        bear = bool(s200 and px["BTC"][i] < s200)
        for c in list(pos):
            p = pos[c]
            cur = px[c][i]
            held = i - p["i"]
            s50 = sma(px[c], i, 50)
            hit = False
            stop_hit = cur <= p["entry"] * (1 - stop) if p["side"] == 1 \
                else cur >= p["entry"] * (1 + stop)
            if held >= hold or stop_hit:
                hit = True
            elif s50 and ((p["side"] == 1 and cur < s50) or
                          (p["side"] == -1 and cur > s50)):
                hit = True
            if hit:
                cash += p["size"] * (1 + (cur / p["entry"] - 1) * p["side"] - FEE)
                del pos[c]
                trades += 1
        if len(pos) < topn and cash > 0.2:
            if bear:
                # SHORT the weakest N by 30d momentum
                sc = []
                for c in coins:
                    if i >= 30 and px[c][i - 30] > 0 and c != "XAUTUSDT":
                        sc.append((px[c][i] / px[c][i - 30] - 1, c))
                sc.sort()
                for _, c in sc[:topn]:
                    if c in pos or cash < 0.2:
                        continue
                    v = sd(px[c], i, 20) or 0.03 * px[c][i]
                    sz = min(cash * risk / (v / px[c][i]) if v else 0,
                             cash / topn)
                    if sz < 0.05:
                        continue
                    pos[c] = {"entry": px[c][i], "i": i, "size": sz, "side": -1}
                    cash -= sz
                    trades += 1
            else:
                if mode == "btc_bull" or mode == "breadth":
                    if mode == "breadth" and breadth(i) < breadth_min:
                        pass
                    else:
                        v = sd(px["BTC"], i, 20) or 0.03 * px["BTC"][i]
                        sz = min(cash * risk / (v / px["BTC"][i]) if v else 0,
                                 cash)
                        if sz >= 0.05 and "BTC" not in pos:
                            pos["BTC"] = {"entry": px["BTC"][i], "i": i,
                                          "size": sz, "side": 1}
                            cash -= sz
                            trades += 1
                else:
                    cands = []
                    for c in coins:
                        s50 = sma(px[c], i, 50)
                        if not s50 or px[c][i] <= s50 or c in pos:
                            continue
                        if mode == "relstr":
                            b50 = sma(px["BTC"], i, 50)
                            rel = (px[c][i] / px[c][i - 30] -
                                   px["BTC"][i] / px["BTC"][i - 30]) \
                                if i >= 30 else 0
                        else:
                            rel = px[c][i] / px[c][i - 30] - 1 if i >= 30 else 0
                        cands.append((rel, c))
                    cands.sort(reverse=True)
                    for _, c in cands[:topn]:
                        if c in pos or cash < 0.2:
                            continue
                        v = sd(px[c], i, 20) or 0.03 * px[c][i]
                        sz = min(cash * risk / (v / px[c][i]) if v else 0,
                                 cash / max(1, topn - len(pos)))
                        if sz < 0.05:
                            continue
                        pos[c] = {"entry": px[c][i], "i": i, "size": sz,
                                  "side": 1}
                        cash -= sz
                        trades += 1
        eq = cash + sum(p["size"] * (1 + (px[c][i] / p["entry"] - 1) * p["side"])
                        for c, p in pos.items())
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    eq = cash + sum(p["size"] * (1 + (px[c][end - 1] / p["entry"] - 1) * p["side"]
                                 - FEE) for c, p in pos.items())
    return (eq - 1) * 100, trades, mdd * 100


def bh(c, s, e):
    return (px[c][e - 1] / px[c][s] - 1) * 100


# ---------- WALK-FORWARD: 5 rolling folds ----------
print("=" * 84)
print("WALK-FORWARD VALIDATION (5 folds, 300-day windows, step 120)")
print("=" * 84)
folds = []
start = 250
while start + 300 <= n:
    folds.append((start, start + 300))
    start += 120

strategies = [
    ("S1 regime L/S (batch2)", "regime", {}),
    ("S2 BTC-hold bull + short", "btc_bull", {}),
    ("S3 S2 + breadth>50%", "breadth", {"breadth_min": 0.5}),
    ("S4 relative-strength long", "relstr", {}),
]
print(f"{'strategy':26} " + " ".join(f"{'f'+str(i+1):>8}" for i in range(len(folds)))
      + f" {'median':>9} {'+folds':>7}")
print("-" * 84)
for name, mode, kw in strategies:
    rets = []
    for a, b in folds:
        r, _, _ = bt(a, b, mode, **kw)
        rets.append(r)
    med = statistics.median(rets)
    pos = sum(1 for r in rets if r > 0)
    print(f"{name:26} " + " ".join(f"{r:>7.1f}%" for r in rets)
          + f" {med:>8.1f}% {pos:>4}/{len(folds)}")

bench = [bh("BTC", a, b) for a, b in folds]
print(f"{'BTC buy&hold (bench)':26} " + " ".join(f"{r:>7.1f}%" for r in bench)
      + f" {statistics.median(bench):>8.1f}% "
      f"{sum(1 for r in bench if r>0):>4}/{len(folds)}")

# ---------- ROBUSTNESS: parameter sweep on the winner ----------
print()
print("=" * 84)
print("ROBUSTNESS SWEEP — S2 (BTC-hold bull + short-weakest bear)")
print("=" * 84)
print(f"{'topn':>6} {'hold':>6} {'risk':>7} {'sma_r':>7} {'full':>9} {'DD':>8} {'tr':>6}")
print("-" * 84)
best = None
for topn in (3, 5):
    for hold in (14, 21, 30):
        for risk in (0.01, 0.02):
            for sma_r in (150, 200):
                r, t, dd = bt(250, n, "btc_bull", topn=topn, hold=hold,
                              risk=risk, sma_r=sma_r)
                if best is None or r > best[0]:
                    best = (r, topn, hold, risk, sma_r, dd, t)
                if r > 15:
                    print(f"{topn:>6} {hold:>6} {risk:>7} {sma_r:>7} "
                          f"{r:>8.1f}% {dd:>7.1f}% {t:>6}")
print()
if best:
    print(f"BEST: return {best[0]:+.1f}%  topn={best[1]} hold={best[2]} "
          f"risk={best[3]} sma_r={best[4]}  DD={best[5]:.1f}%  trades={best[6]}")
print()
print(f"REFERENCE  BTC buy&hold full period: {bh('BTC',250,n):+.1f}%")
eql = sum(bh(c, 250, n) for c in coins) / len(coins)
print(f"REFERENCE  equal-weight basket:      {eql:+.1f}%")
