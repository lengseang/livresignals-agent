"""EDGE HUNT BATCH 2 — the four ideas that batch 1 pointed to.

1. SHORT-SIDE SELECTION   - in bear regimes, short the WEAKEST, not random
2. VOL-SCALED SIZING      - risk parity: smaller size when vol is high
3. REGIME-CONDITIONAL     - bull: long winners / bear: short losers
4. FUNDING-AWARE SHORTS   - prefer shorts that COLLECT funding

Method unchanged (and non-negotiable): shared-account sim, real fees,
IS/OOS split, benchmark against BTC and the equal-weight basket.
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


def btc_above200(i):
    s = sma(px["BTC"], i, 200)
    return bool(s and px["BTC"][i] > s)


def bt(start, end, mode, topn=3, hold=14, **kw):
    cash, pos = 1.0, {}
    peak, mdd, trades = 1.0, 0.0, 0
    for i in range(max(start, 220), end):
        for c in list(pos):
            p = pos[c]
            cur = px[c][i]
            held = i - p["i"]
            s = sma(px[c], i, 50)
            hit = False
            if mode == "regime_ls":
                if (p["side"] == 1 and s and cur < s) or \
                   (p["side"] == -1 and s and cur > s) or held >= hold:
                    hit = True
            elif mode in ("vol_scaled", "bear_short", "funding_short"):
                if held >= hold:
                    hit = True
                elif p["side"] == 1 and px[c][i] < p["entry"] * 0.90:
                    hit = True
                elif p["side"] == -1 and px[c][i] > p["entry"] * 1.10:
                    hit = True
            if hit:
                cash += p["size"] * (1 + (cur / p["entry"] - 1) * p["side"]
                                     - FEE)
                del pos[c]
                trades += 1
        # ---- entries ----
        bear = not btc_above200(i)
        if mode == "regime_ls":
            if len(pos) < topn and cash > 0.2:
                sc = []
                for c in coins:
                    if i >= 30 and px[c][i - 30] > 0:
                        sc.append((px[c][i] / px[c][i - 30] - 1, c))
                sc.sort(reverse=True)
                want = sc[:topn] if not bear else [(m, c) for m, c in sc[-topn:]]
                for _, c in want:
                    if c in pos or cash < 0.2:
                        continue
                    sz = cash / max(1, topn - len(pos))
                    side = -1 if bear else 1
                    pos[c] = {"entry": px[c][i], "i": i, "size": sz, "side": side}
                    cash -= sz
                    trades += 1
        elif mode == "bear_short":
            if bear and len(pos) < topn and cash > 0.2:
                sc = []
                for c in coins:
                    if i >= 30 and px[c][i - 30] > 0:
                        sc.append((px[c][i] / px[c][i - 30] - 1, c))
                sc.sort()
                for _, c in sc[:topn]:
                    if c in pos:
                        continue
                    sz = cash / topn
                    pos[c] = {"entry": px[c][i], "i": i, "size": sz, "side": -1}
                    cash -= sz
                    trades += 1
        elif mode == "vol_scaled":
            if not bear and cash > 0.2:
                for c in coins:
                    if len(pos) >= topn or c in pos:
                        break
                    s = sma(px[c], i, 50)
                    v = sd(px[c], i, 20)
                    if not s or not v or px[c][i] <= s:
                        continue
                    # risk parity: target constant $ risk per position
                    tgt_risk = 0.02          # 2% of cash at risk per position
                    sz = cash * tgt_risk / (v / px[c][i]) if v > 0 else 0
                    sz = min(sz, cash / topn)
                    if sz < 0.05:
                        continue
                    pos[c] = {"entry": px[c][i], "i": i, "size": sz, "side": 1}
                    cash -= sz
                    trades += 1
        eq = cash + sum(p["size"] * (1 + (px[c][i] / p["entry"] - 1) * p["side"])
                        for c, p in pos.items())
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    eq = cash + sum(p["size"] * (1 + (px[c][end - 1] / p["entry"] - 1) * p["side"]
                                 - FEE) for c, p in pos.items())
    return (eq - 1) * 100, trades, mdd * 100


def buyhold(c, s, e):
    return (px[c][e - 1] / px[c][s] - 1) * 100


# BTC's own drawdown for risk-adjusted comparison
bp, bdd = 1.0, 0.0
for i in range(n):
    bp = max(bp, px["BTC"][i] / px["BTC"][0])
    bdd = min(bdd, (px["BTC"][i] / px["BTC"][0]) / bp - 1)

print(f"window {n}d | IS 0-{mid} | OOS {mid}-{n}")
print(f"BENCH  BTC: IS {buyhold('BTC',0,mid):+.1f}% OOS {buyhold('BTC',mid,n):+.1f}%"
      f" full DD {bdd*100:.1f}%")
print(f"BENCH  EQL: IS {sum(buyhold(c,0,mid) for c in coins)/len(coins):+.1f}%"
      f" OOS {sum(buyhold(c,mid,n) for c in coins)/len(coins):+.1f}%")
print("=" * 78)
print(f"{'strategy':28} {'IS':>8} {'OOS':>8} {'OOS DD':>8} {'full':>9} {'r/DD':>6} {'tr':>5}")
print("=" * 78)
for name, mode, kw in [
        ("1. regime_ls top3/14d", "regime_ls", {"topn": 3, "hold": 14}),
        ("1b. regime_ls top5/14d", "regime_ls", {"topn": 5, "hold": 14}),
        ("2. vol-scaled long", "vol_scaled", {"topn": 3}),
        ("3. bear-short only", "bear_short", {"topn": 3, "hold": 14}),
]:
    is_r, is_t, _ = bt(0, mid, mode, **kw)
    oos_r, oos_t, oos_dd = bt(mid, n, mode, **kw)
    full = ((1 + is_r / 100) * (1 + oos_r / 100) - 1) * 100
    rdd = full / abs(oos_dd) if oos_dd else 0
    flag = "OK" if full > 0 else "  "
    print(f"{name:28} {is_r:>7.1f}% {oos_r:>7.1f}% {oos_dd:>7.1f}% "
          f"{full:>8.1f}% {rdd:>5.2f} {oos_t:>5} {flag}")
