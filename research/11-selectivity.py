"""CAN SELECTIVITY RAISE WIN RATE? (the honest alternative to 'test more')

Do NOT hunt for another config - that is how you overfit. Instead: record
each trade's ENTRY FEATURES, then ask whether some feature separates winners
from losers. If it does, we publish only the good ones and win rate rises
WITHOUT touching the payoff ratio.

Features recorded at entry:
  mom30     - 30-day momentum of the coin
  atr_pct   - volatility at entry
  dist_sma  - how far above its SMA50 price was
  breadth   - % of the universe above their own SMA50
  btc_mom   - BTC's own 30-day momentum (market backdrop)
  rank      - was it the #1 momentum pick, #2, or #3
  is_gold   - the diversifier slot
  bear      - regime at entry
"""
import json
import statistics
from collections import defaultdict

FEE = 0.00144
gold = {int(k): float(v) for k, v in
        json.load(open("/opt/trading-agent/research/price-cache-gold-5y.json")).items()}
d = json.load(open("/opt/trading-agent/research/price-cache-1000d.json"))
d = {c: {int(k): v for k, v in dd.items()} for c, dd in d.items()}
d.pop("MATIC", None)
d["XAUT"] = gold
days = sorted(set.intersection(*[set(x) for x in d.values()]))
px = {c: [d[c][t] for t in days] for c in d}
coins = sorted(d)
n = len(days)


def sma(a, i, k):
    w = a[max(0, i - k):i]
    return sum(w) / len(w) if len(w) >= k else None


def atr(i, c, k=14):
    return (sum(abs(px[c][j] - px[c][j - 1])
                for j in range(max(1, i - k), i)) / k) or px[c][i] * 0.03


def breadth(i):
    up = tot = 0
    for c in coins:
        s = sma(px[c], i, 50)
        if s:
            tot += 1
            up += px[c][i] > s
    return up / tot if tot else 0.5


def collect(start, end, stop_mult=2.5, tp_r=2.0, topn=3, hold=21, risk=0.01):
    """Run the live logic and RECORD every trade with its entry features."""
    cash, pos = 1.0, {}
    trades = []
    for i in range(max(start, 220), end):
        s200 = sma(px["BTC"], i, 150)
        bear = bool(s200 and px["BTC"][i] < s200)
        for c in list(pos):
            p = pos[c]
            cur = px[c][i]
            held = i - p["i"]
            s50 = sma(px[c], i, 50)
            R = p["risk_pct"]
            move = (cur / p["entry"] - 1) * p["side"]
            hit = held >= hold
            if p["side"] == 1:
                if cur <= p["stop"]:
                    hit = True
                elif tp_r and move >= tp_r * R:
                    hit = True
                elif s50 and cur < s50:
                    hit = True
            else:
                if cur >= p["stop"]:
                    hit = True
                elif tp_r and move >= tp_r * R:
                    hit = True
                elif s50 and cur > s50:
                    hit = True
            if hit:
                r = (cur / p["entry"] - 1) * p["side"] - FEE
                cash += p["size"] * (1 + r)
                p["ret"] = r
                p["won"] = r > 0
                trades.append(p)
                del pos[c]
        if len(pos) < topn and cash > 0.2:
            if bear:
                cands = []
                for c in coins:
                    if i >= 30 and px[c][i - 30] > 0:
                        cands.append((px[c][i] / px[c][i - 30] - 1, c))
                cands.sort()
                picks = [(c, -1) for _, c in cands[:topn]]
            else:
                cands = []
                for c in coins:
                    s50 = sma(px[c], i, 50)
                    if not s50 or px[c][i] <= s50:
                        continue
                    mom = px[c][i] / px[c][i - 30] - 1 if i >= 30 else 0
                    cands.append((mom, c))
                cands.sort(reverse=True)
                picks = [(c, 1) for _, c in cands[:topn]]
            for rank_i, (c, side) in enumerate(picks):
                if c in pos or cash < 0.2:
                    continue
                vol = atr(i, c) / px[c][i]
                rp = min(stop_mult * vol, 0.35)
                sz = min(cash * risk / vol if vol else 0, cash / topn)
                if sz < 0.05:
                    continue
                s50 = sma(px[c], i, 50) or px[c][i]
                bm = px["BTC"][i] / px["BTC"][i - 30] - 1 if i >= 30 else 0
                pos[c] = {"symbol": c, "side": side, "entry": px[c][i], "i": i,
                          "size": sz, "stop": px[c][i] * (1 - rp) if side == 1
                          else px[c][i] * (1 + rp), "risk_pct": rp,
                          "mom30": (px[c][i] / px[c][i - 30] - 1) if i >= 30 else 0,
                          "atr_pct": vol, "dist_sma": px[c][i] / s50 - 1,
                          "breadth": breadth(i), "btc_mom": bm, "rank": rank_i,
                          "is_gold": c == "XAUT", "bear": bear}
                cash -= sz
    return trades


folds = []
f_size = min(280, max(140, (n - 240) // 3))
s = 220
while s + f_size <= n:
    folds.append((s, s + f_size))
    s += f_size - 40

allt = []
for a, b in folds:
    allt += collect(a, b)

print(f"total trades recorded: {len(allt)}")
base_wr = sum(1 for t in allt if t["won"]) / len(allt) * 100
base_exp = statistics.mean([t["ret"] for t in allt]) * 100
print(f"BASELINE: WR {base_wr:.1f}%  expectancy {base_exp:+.3f}% per trade")
print()


def bucket_report(fkey, label, nbuckets=3):
    vals = sorted(t[fkey] for t in allt)
    cuts = [vals[int(len(vals) * k / nbuckets)] for k in range(1, nbuckets)]
    print(f"--- by {label} ({nbuckets} buckets) ---")
    for b in range(nbuckets):
        lo = cuts[b - 1] if b > 0 else -1e9
        hi = cuts[b] if b < nbuckets - 1 else 1e9
        grp = [t for t in allt if lo <= t[fkey] < hi]
        if not grp:
            continue
        wr = sum(1 for t in grp if t["won"]) / len(grp) * 100
        ex = statistics.mean([t["ret"] for t in grp]) * 100
        w = [t["ret"] for t in grp if t["won"]]
        l = [t["ret"] for t in grp if not t["won"]]
        pf = (statistics.mean(w) / abs(statistics.mean(l))) if w and l else 0
        tag = "<-- BEST" if wr == max(
            sum(1 for t in allt if (cuts[bb - 1] if bb > 0 else -1e9)
                <= t[fkey] < (cuts[bb] if bb < nbuckets - 1 else 1e9)
                and t["won"]) / max(1, len([t for t in allt
                if (cuts[bb - 1] if bb > 0 else -1e9) <= t[fkey]
                < (cuts[bb] if bb < nbuckets - 1 else 1e9)])) * 100
            for bb in range(nbuckets)) else ""
        print(f"   {label} [{lo:>8.3f} .. {hi:>8.3f})  n={len(grp):>3}  "
              f"WR {wr:>5.1f}%  payoff {pf:.2f}  exp {ex:+.3f}%  {tag}")
    print()


bucket_report("mom30", "momentum")
bucket_report("atr_pct", "volatility")
bucket_report("dist_sma", "distance above SMA50")
bucket_report("breadth", "market breadth")
bucket_report("btc_mom", "BTC momentum")
bucket_report("rank", "momentum rank", nbuckets=3)

print("--- by asset class ---")
for lbl, grp in [("crypto", [t for t in allt if not t["is_gold"]]),
                 ("GOLD", [t for t in allt if t["is_gold"]])]:
    if grp:
        wr = sum(1 for t in grp if t["won"]) / len(grp) * 100
        ex = statistics.mean([t["ret"] for t in grp]) * 100
        print(f"   {lbl:8} n={len(grp):>3}  WR {wr:>5.1f}%  exp {ex:+.3f}%")
print()
print("--- by regime ---")
for lbl, grp in [("BULL", [t for t in allt if not t["bear"]]),
                 ("BEAR", [t for t in allt if t["bear"]])]:
    if grp:
        wr = sum(1 for t in grp if t["won"]) / len(grp) * 100
        ex = statistics.mean([t["ret"] for t in grp]) * 100
        print(f"   {lbl:8} n={len(grp):>3}  WR {wr:>5.1f}%  exp {ex:+.3f}%")
