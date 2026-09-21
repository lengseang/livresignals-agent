"""CAN WE GET WR >= 50% *AND* POSITIVE EXPECTANCY?

Benchmark: professional trend followers run 35-45% win rates. But a SELLABLE
signal feed wants higher - subscribers judge accuracy, not expectancy.

Four levers:
  1. STOP WIDTH  - wider stops = fewer stop-outs = higher WR
                   (each loss bigger - the classic trade-off)
  2. SCALE-OUT   - book half at 1R, stop to breakeven, run the rest to 3R
  3. ENTRY TYPE  - momentum breakout (current) vs pullback inside uptrend
  4. TP DISTANCE - 1R kills it, 2R works; re-check with the other levers

Accounting (fixed): on entry cash -= orig. Partial exits add proceeds to
cash AND to `realized`. Trade pnl = realized + final_proceeds - orig.
A trade counts as a WIN only if its NET pnl is positive. No fudging.
"""
import json
import statistics

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


def bt(start, end, stop_mult=2.5, tp_r=2.0, scale_out=False, entry="momentum",
       topn=3, hold=21, risk=0.01, be_at_r=None):
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
            R = p["risk_pct"]
            move = (cur / p["entry"] - 1) * p["side"]
            # ---- scale-out: book half at 1R, stop to breakeven ----
            if scale_out and not p.get("half") and move >= R:
                qty = p["remaining"] * 0.5
                proceeds = qty * (1 + R - FEE)
                cash += proceeds
                p["realized"] += proceeds
                p["remaining"] -= qty
                p["half"] = True
                p["stop"] = p["entry"]
            # ---- breakeven move (no scale-out) ----
            elif be_at_r and not p.get("be") and move >= be_at_r * R:
                p["stop"] = p["entry"]
                p["be"] = True
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
                proceeds = p["remaining"] * (1 + r)
                cash += proceeds
                pnl = p["realized"] + proceeds - p["orig"]
                (wins if pnl > 0 else losses).append(pnl / p["orig"])
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
                    if not s50:
                        continue
                    if entry == "momentum":
                        if px[c][i] <= s50:
                            continue
                        score = px[c][i] / px[c][i - 30] - 1 if i >= 30 else 0
                    else:
                        m5 = sma(px[c], i, 5)
                        if px[c][i] <= s50 or not m5 or px[c][i] > m5:
                            continue
                        score = (m5 / px[c][i] - 1)
                    cands.append((score, c))
                cands.sort(reverse=True)
                picks = [(c, 1) for _, c in cands[:topn]]
            for c, side in picks:
                if c in pos or cash < 0.2:
                    continue
                vol = atr(i, c) / px[c][i]
                rp = min(stop_mult * vol, 0.35)
                sz = min(cash * risk / vol if vol else 0, cash / topn)
                if sz < 0.05:
                    continue
                st = px[c][i] * (1 - rp) if side == 1 else px[c][i] * (1 + rp)
                pos[c] = {"entry": px[c][i], "i": i, "remaining": sz,
                          "orig": sz, "realized": 0.0, "side": side,
                          "stop": st, "risk_pct": rp}
                cash -= sz
        eq = cash + sum(p["remaining"] *
                        (1 + (px[c][i] / p["entry"] - 1) * p["side"])
                        for c, p in pos.items())
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    eq = cash + sum(p["remaining"] *
                    (1 + (px[c][end - 1] / p["entry"] - 1) * p["side"] - FEE)
                    for c, p in pos.items())
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

# sanity: no-TP / no-scale-out / momentum must roughly match earlier runs
print("SANITY CHECK: baseline should be ~+1% median, ~33% WR (matches batch 4)")
r = [bt(a, b) for a, b in folds]
print(f"  baseline -> median {statistics.median([x[0] for x in r]):+.1f}%, "
      f"WR {statistics.mean([x[1] for x in r]):.0f}%, "
      f"trades {int(statistics.mean([x[5] for x in r]))}")
print()
print(f"window {n}d | {len(folds)} folds")
print("=" * 102)
print(f"{'variant':38} {'med ret':>8} {'+fld':>5} {'WR':>6} {'payoff':>7} "
      f"{'exp/tr':>9} {'DD':>7} {'tr':>5}")
print("-" * 102)
tests = [
    ("current (2.5ATR stop, TP2R)", {}),
    ("wide stop 4ATR", {"stop_mult": 4.0}),
    ("wide stop 6ATR", {"stop_mult": 6.0}),
    ("breakeven stop at 1R + TP2R", {"be_at_r": 1.0}),
    ("SCALE-OUT half@1R, run to 3R", {"scale_out": True, "tp_r": 3.0}),
    ("SCALE-OUT half@1R, run to 2R", {"scale_out": True, "tp_r": 2.0}),
    ("SCALE-OUT + wide 4ATR", {"scale_out": True, "tp_r": 3.0, "stop_mult": 4.0}),
    ("pullback entry", {"entry": "pullback"}),
    ("pullback + wide 4ATR", {"entry": "pullback", "stop_mult": 4.0}),
    ("pullback + scale-out", {"entry": "pullback", "scale_out": True, "tp_r": 3.0}),
]
for name, kw in tests:
    rr, wrs, pfs, exps, dds, trs = [], [], [], [], [], []
    for a, b in folds:
        ret, wr, pf, ex, dd, tr = bt(a, b, **kw)
        rr.append(ret)
        if tr:
            wrs.append(wr); pfs.append(pf); exps.append(ex); trs.append(tr)
        dds.append(dd)
    med = statistics.median(rr)
    wr_m = statistics.mean(wrs) if wrs else 0
    ex_m = statistics.mean(exps) if exps else 0
    flag = " <<< WR>=50 & exp>0" if wr_m >= 50 and ex_m > 0 else ""
    print(f"{name:38} {med:>7.1f}% {sum(1 for x in rr if x>0):>3}/{len(folds)} "
          f"{wr_m:>5.0f}% {(statistics.mean(pfs) if pfs else 0):>7.2f} "
          f"{ex_m:>8.3f}% {min(dds):>6.1f}% "
          f"{int(statistics.mean(trs)) if trs else 0:>5}{flag}")
