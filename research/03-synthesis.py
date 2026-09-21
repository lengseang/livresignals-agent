"""THE SYNTHESIS — regime-adaptive vol-scaled system.

Batch 2 winners:
  #2 vol-scaled long : +2.8% IS / +7.2% OOS, DD -20.1%, 47 trades
  #3 bear-short only : -21.6% IS / +32.5% OOS

Neither is regime-complete alone. #2 only goes long (dead in bears),
#3 only shorts (dead in bulls). COMBINE them:

    BULL (BTC>200sma):  long coins > 50sma, risk-parity sized
    BEAR (BTC<200sma):  short the WEAKEST N by 30d momentum

Also test: vol-scaled sizing on the short side too, and a flat
"risk-parity only" variant with no regime gate.
"""
import json

d = json.load(open("/tmp/edge_data.json"))
d = {c: {int(k): v for k, v in dd.items()} for c, dd in d.items()}
d.pop("MATIC", None)
days = sorted(set.intersection(*[set(x) for x in d.values()]))
px = {c: [d[c][t] for t in days] for c in d}
coins = sorted(d)
n = len(days)
mid = int(n * 0.6)
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


def bt(start, end, topn=3, hold=21, stop=0.10, risk=0.02, use_regime=True):
    cash, pos = 1.0, {}
    peak, mdd, trades = 1.0, 0.0, 0
    for i in range(max(start, 220), end):
        bear = False
        s200 = sma(px["BTC"], i, 200)
        if s200:
            bear = px["BTC"][i] < s200
        for c in list(pos):
            p = pos[c]
            cur = px[c][i]
            held = i - p["i"]
            s50 = sma(px[c], i, 50)
            hit = False
            if p["side"] == 1:
                if held >= hold or cur < p["entry"] * (1 - stop):
                    hit = True
                elif s50 and cur < s50:
                    hit = True
            else:
                if held >= hold or cur > p["entry"] * (1 + stop):
                    hit = True
                elif s50 and cur > s50:
                    hit = True
            if hit:
                cash += p["size"] * (1 + (cur / p["entry"] - 1) * p["side"] - FEE)
                del pos[c]
                trades += 1
        want_short = bear if use_regime else False
        if len(pos) < topn and cash > 0.2:
            if want_short:
                sc = []
                for c in coins:
                    if i >= 30 and px[c][i - 30] > 0:
                        sc.append((px[c][i] / px[c][i - 30] - 1, c))
                sc.sort()
                picks = [(c, -1) for _, c in sc[:topn]]
            else:
                picks = []
                for c in coins:
                    s50 = sma(px[c], i, 50)
                    if s50 and px[c][i] > s50:
                        v = sd(px[c], i, 20)
                        if v and v > 0:
                            mom = px[c][i] / px[c][i - 30] - 1 if i >= 30 else 0
                            picks.append((c, 1, v, mom))
                picks.sort(key=lambda x: -x[3])
                picks = [(c, 1) for c, s, v, m in picks[:topn]]
            for c, side in picks:
                if c in pos or cash < 0.2:
                    continue
                v = sd(px[c], i, 20) or 0.03 * px[c][i]
                sz = cash * risk / (v / px[c][i]) if v > 0 else 0
                sz = min(sz, cash / max(1, topn - len(pos)))
                if sz < 0.05:
                    continue
                pos[c] = {"entry": px[c][i], "i": i, "size": sz, "side": side}
                cash -= sz
                trades += 1
        eq = cash + sum(p["size"] * (1 + (px[c][i] / p["entry"] - 1) * p["side"])
                        for c, p in pos.items())
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    eq = cash + sum(p["size"] * (1 + (px[c][end - 1] / p["entry"] - 1) * p["side"] - FEE)
                    for c, p in pos.items())
    return (eq - 1) * 100, trades, mdd * 100


def bh(c, s, e):
    return (px[c][e - 1] / px[c][s] - 1) * 100


bp, bdd = 1.0, 0.0
for i in range(n):
    bp = max(bp, px["BTC"][i] / px["BTC"][0])
    bdd = min(bdd, (px["BTC"][i] / px["BTC"][0]) / bp - 1)

print(f"window {n}d | IS 0-{mid} | OOS {mid}-{n}")
print(f"BENCH BTC : IS {bh('BTC',0,mid):+7.1f}% OOS {bh('BTC',mid,n):+7.1f}%"
      f"  full DD {bdd*100:.1f}%")
print(f"BENCH EQL : IS {sum(bh(c,0,mid) for c in coins)/len(coins):+7.1f}%"
      f" OOS {sum(bh(c,mid,n) for c in coins)/len(coins):+7.1f}%")
print("=" * 78)
print(f"{'strategy':30} {'IS':>8} {'OOS':>8} {'DD':>8} {'full':>9} {'r/DD':>6} {'tr':>5}")
print("=" * 78)
for name, kw in [
        ("regime-adaptive t3/h21", {"topn": 3, "hold": 21}),
        ("regime-adaptive t3/h10", {"topn": 3, "hold": 10}),
        ("regime-adaptive t5/h21", {"topn": 5, "hold": 21}),
        ("no-regime (long only) t3", {"topn": 3, "hold": 21, "use_regime": False}),
        ("regime t3 risk0.01", {"topn": 3, "hold": 21, "risk": 0.01}),
]:
    is_r, is_t, _ = bt(0, mid, **kw)
    oos_r, oos_t, oos_dd = bt(mid, n, **kw)
    full = ((1 + is_r / 100) * (1 + oos_r / 100) - 1) * 100
    rdd = full / abs(oos_dd) if oos_dd else 0
    flag = "OK" if (full > 0 and oos_r > 0) else ("  " if full > 0 else "XX")
    print(f"{name:30} {is_r:>7.1f}% {oos_r:>7.1f}% {oos_dd:>7.1f}% "
          f"{full:>8.1f}% {rdd:>5.2f} {oos_t:>5} {flag}")
