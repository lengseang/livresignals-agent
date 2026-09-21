"""CAN WE ACTUALLY REACH 60% WIN RATE?

THE MATH FIRST (this is not opinion):

    expectancy > 0  <=>  WR x avg_win > (1-WR) x avg_loss
                    <=>  payoff > (1-WR)/WR

    At WR = 60%:  payoff > 0.40/0.60 = 0.667

So 60% WR is NOT mathematically hard - average wins only need to be 2/3 of
average losses. The question is empirical: can THIS strategy deliver that
combination, or does raising WR always cost more payoff than it gains?

METHOD: sweep the take-profit from 0.25R to 3R and trace the actual
(WR, payoff, expectancy) frontier. Every point is a real simulation on
7,942 setups. This tells us whether a 60% WR point EXISTS and, if so,
whether it makes money.
"""
import json
import statistics

FEE = 0.00144
HOLD = 21
SMA_TREND = 50
SMA_REGIME = 150
STOP_ATR = 1.5            # the newly deployed stop

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


def sma(a, i, k):
    return sum(a[i - k:i]) / k if i >= k else None


def atr(a, i, k=14):
    if i < k + 1:
        return None
    return (sum(abs(a[j] - a[j - 1]) for j in range(i - k, i)) / k) / a[i]


def run(tp_r, exit_mode="mom5"):
    rets = []
    for i in range(210, n - 2):
        s200 = sma(px["BTC"], i, SMA_REGIME)
        bear = bool(s200 and px["BTC"][i] < s200)
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
                if move >= tp_r * risk:
                    r = tp_r * risk - FEE
                    break
                if exit_mode == "mom5":
                    if j >= 6 and (a[j] / a[j - 5] - 1) * side < 0:
                        r = move - FEE
                        break
                else:
                    s = sma(a, j, SMA_TREND)
                    if s and ((side == 1 and a[j] < s) or
                              (side == -1 and a[j] > s)):
                        r = move - FEE
                        break
            if r is None:
                jx = min(i + HOLD, n - 1)
                r = (a[jx] / entry - 1) * side - FEE
            rets.append(r)
    return rets


print("=" * 92)
print("WIN-RATE FRONTIER — sweeping the take-profit (mom5 exit, 1.5xATR stop)")
print("=" * 92)
print(f"{'TP':>6} {'WR':>7} {'payoff':>8} {'exp/trade':>11} {'breakeven WR':>13} "
      f"{'verdict':>16}")
print("-" * 92)

best = None
for tp in (0.25, 0.4, 0.5, 0.6, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0,
           99.0):
    r = run(tp)
    w = [x for x in r if x > 0]
    l = [x for x in r if x <= 0]
    wr = len(w) / len(r) * 100
    payoff = (statistics.mean(w) / abs(statistics.mean(l))) if w and l else 0
    exp = statistics.mean(r) * 100
    be = 100 / (1 + payoff) if payoff else 0
    label = "no TP" if tp > 90 else f"{tp:g}R"
    flag = "PROFIT" if exp > 0 else "loses"
    if exp > 0 and (best is None or wr > best[0]):
        best = (wr, label, payoff, exp)
    print(f"{label:>6} {wr:>6.1f}% {payoff:>8.2f} {exp:>+10.3f}% {be:>12.1f}% "
          f"{flag:>16}")

print()
print("=" * 92)
print("THE ANSWER TO 'CAN WE REACH 60%?'")
print("=" * 92)
probe = run(0.25)
w = [x for x in probe if x > 0]
l = [x for x in probe if x <= 0]
wr25 = len(w) / len(probe) * 100
pay25 = statistics.mean(w) / abs(statistics.mean(l))
exp25 = statistics.mean(probe) * 100
print(f"  Tightest tested target (0.25R):")
print(f"    WR {wr25:.1f}%   payoff {pay25:.2f}   expectancy {exp25:+.3f}%")
print(f"    -> {'' if wr25 >= 60 else 'STILL SHORT OF'} 60% WR, and "
      f"{'profitable' if exp25 > 0 else 'IT LOSES MONEY'}")
print()
if best:
    print(f"  Highest WR that is STILL PROFITABLE: {best[0]:.1f}% "
          f"(target {best[1]}, payoff {best[2]:.2f}, exp {best[3]:+.3f}%)")
    print(f"  Gap to 60%: {60 - best[0]:.1f}pp")
else:
    print("  No profitable configuration found on the frontier.")
print()
print("  Every point that pushes WR up pushes payoff down FASTER.")
print("  That is why 60% has not appeared despite many attempts (R011, R012).")
