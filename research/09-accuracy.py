"""THE ACCURACY PROBLEM — can we raise win rate without killing expectancy?

Batch 4 exposed this: our strategy wins only ~30% of the time. It is still
profitable (payoff 2.2 makes up for it), but a signal provider selling a
30%-win-rate feed will lose subscribers even if the maths is sound.

Subscribers judge ACCURACY. Pros judge EXPECTANCY. We need both.

Test: add take-profit targets (raises WR, lowers payoff) and see where the
sweet spot is. Also test trailing stops, which try to keep both.
"""
import json
import statistics

FEE = 0.00144
CACHE = "/opt/trading-agent/research/price-cache-gold-5y.json"
gold = {int(k): float(v) for k, v in json.load(open(CACHE)).items()}
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
    out = [abs(px[c][j] - px[c][j - 1]) for j in range(max(1, i - k), i)]
    return sum(out) / len(out) if out else px[c][i] * 0.03


def bt(start, end, tp_r=None, trail=False, topn=3, hold=21, risk=0.01,
       corr_size=True):
    cash, pos = 1.0, {}
    peak, mdd = 1.0, 0.0
    wins, losses = [], []
    for i in range(max(start, 220), end):
        s200 = sma(px["BTC"], i, 150)
        bear = bool(s200 and px["BTC"][i] < s200)
        for c in list(pos):
            p = pos[c]
            cur = px[c][i]
            held = i - p["i"]
            s50 = sma(px[c], i, 50)
            if trail and p["side"] == 1:
                p["stop"] = max(p["stop"], cur * (1 - 2.5 * atr(i, c) / cur))
            if trail and p["side"] == -1:
                p["stop"] = min(p["stop"], cur * (1 + 2.5 * atr(i, c) / cur))
            hit = held >= hold
            if p["side"] == 1:
                if cur <= p["stop"]:
                    hit = True
                elif tp_r and cur >= p["entry"] * (1 + tp_r * p["risk_pct"]):
                    hit = True
                elif s50 and cur < s50:
                    hit = True
            else:
                if cur >= p["stop"]:
                    hit = True
                elif tp_r and cur <= p["entry"] * (1 - tp_r * p["risk_pct"]):
                    hit = True
                elif s50 and cur > s50:
                    hit = True
            if hit:
                r = (cur / p["entry"] - 1) * p["side"] - FEE
                cash += p["size"] * (1 + r)
                (wins if r > 0 else losses).append(r)
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
            scale = 1.0
            if corr_size and picks:
                k = len(picks)
                rho = 0.5 if any(c == "XAUT" for c, _ in picks) else 0.65
                scale = 1.0 / (1 + (k - 1) * rho)
            for c, side in picks:
                if c in pos or cash < 0.2:
                    continue
                vol = atr(i, c) / px[c][i]
                sz = min(cash * risk * scale / vol if vol else 0,
                         cash * scale / topn)
                if sz < 0.05:
                    continue
                risk_pct = 2.5 * vol
                st = px[c][i] * (1 - risk_pct) if side == 1 else \
                    px[c][i] * (1 + risk_pct)
                pos[c] = {"entry": px[c][i], "i": i, "size": sz, "side": side,
                          "stop": st, "risk_pct": risk_pct}
                cash -= sz
        eq = cash + sum(p["size"] * (1 + (px[c][i] / p["entry"] - 1) * p["side"])
                        for c, p in pos.items())
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    eq = cash + sum(p["size"] * (1 + (px[c][end - 1] / p["entry"] - 1) * p["side"]
                                 - FEE) for c, p in pos.items())
    tr = wins + losses
    wr = len(wins) / len(tr) * 100 if tr else 0
    payoff = (statistics.mean(wins) / abs(statistics.mean(losses))) \
        if wins and losses else 0
    exp_ = statistics.mean(tr) * 100 if tr else 0
    return (eq - 1) * 100, wr, payoff, exp_, mdd * 100, len(tr)


folds = []
f_size = min(280, max(140, (n - 240) // 3))
s = 220
while s + f_size <= n:
    folds.append((s, s + f_size))
    s += f_size - 40

print(f"window {n}d | {len(folds)} folds | {len(coins)} assets")
print("=" * 92)
print(f"{'variant':30} {'med ret':>8} {'+fld':>5} {'WR':>6} {'payoff':>7} "
      f"{'exp/tr':>9} {'DD':>7} {'trades':>7}")
print("-" * 92)
for name, kw in [
        ("no TP (current style)", {}),
        ("TP at 1R", {"tp_r": 1.0}),
        ("TP at 1.5R", {"tp_r": 1.5}),
        ("TP at 2R", {"tp_r": 2.0}),
        ("TP at 3R", {"tp_r": 3.0}),
        ("trailing stop", {"trail": True}),
        ("TP 2R + corr size off", {"tp_r": 2.0, "corr_size": False}),
]:
    rr, wrs, pfs, exps, dds, trs = [], [], [], [], [], []
    for a, b in folds:
        r, wr, pf, ex, dd, tr = bt(a, b, **kw)
        rr.append(r)
        if tr:
            wrs.append(wr)
            pfs.append(pf)
            exps.append(ex)
            trs.append(tr)
        dds.append(dd)
    med = statistics.median(rr)
    print(f"{name:30} {med:>7.1f}% {sum(1 for x in rr if x>0):>3}/{len(folds)} "
          f"{(statistics.mean(wrs) if wrs else 0):>5.0f}% "
          f"{(statistics.mean(pfs) if pfs else 0):>7.2f} "
          f"{(statistics.mean(exps) if exps else 0):>8.3f}% "
          f"{min(dds):>6.1f}% {int(statistics.mean(trs)) if trs else 0:>7}")
