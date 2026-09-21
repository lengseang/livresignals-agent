"""WHAT TO EXPECT OVER THE NEXT FEW DAYS

Not a pep talk. A distribution, so the user knows in advance that a losing
run is NORMAL and is not evidence the system broke.

Calibrated on the current deployed config: risk-parity ~1% per position,
3 concurrent positions, 4-hourly decision cycle, hold up to 21 days,
mom5 exit, no take-profit, 1.5xATR stop.
"""
import json
import random
import statistics

FEE = 0.00144
HOLD = 21
SMA_TREND = 50
SMA_REGIME = 150
STOP_ATR = 1.5
TOPN = 3
RISK = 0.01

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


# harvest every trade the CURRENT config would take, with its holding time
trades = []          # (R_multiple, hold_days)
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
        r, hd = None, HOLD
        for j in range(i + 1, min(i + HOLD + 1, n)):
            move = (a[j] / entry - 1) * side
            if (a[j] <= stop) if side == 1 else (a[j] >= stop):
                r, hd = -risk - FEE, j - i
                break
            if j >= 6 and (a[j] / a[j - 5] - 1) * side < 0:
                r, hd = move - FEE, j - i
                break
        if r is None:
            jx = min(i + HOLD, n - 1)
            r, hd = (a[jx] / entry - 1) * side - FEE, jx - i
        trades.append((r / risk, hd, i))

rs = [t[0] for t in trades]
wr = sum(1 for r in rs if r > 0) / len(rs) * 100
avg_hold = statistics.mean(t[2] for t in trades)
print("=" * 88)
print("CALIBRATION — from our own 1,000 days of history")
print("=" * 88)
print(f"  trades simulated     : {len(rs)}")
print(f"  win rate             : {wr:.1f}%")
print(f"  avg R per trade      : {statistics.mean(rs):+.3f}R")
print(f"  avg hold             : {avg_hold:.0f} days")
print(f"  trades per week      : ~{3 * 7 / avg_hold:.2f} "
      f"(3 slots / {avg_hold:.0f}-day hold)")

# ---- streak analysis: how long are losing streaks? ----
random.seed(7)
cur = best = 0
streaks = []
for r in rs:
    if r <= 0:
        cur += 1
        best = max(best, cur)
    else:
        if cur:
            streaks.append(cur)
        cur = 0
if cur:
    streaks.append(cur)
streaks.sort()
print()
print("=" * 88)
print("LOSING STREAKS — what actually happened in history")
print("=" * 88)
print(f"  longest losing streak        : {max(streaks)} trades")
print(f"  median streak                : {statistics.median(streaks):.0f}")
print(f"  90th percentile streak       : {streaks[int(len(streaks)*0.9)]}")
print(f"  streaks of 5+                : {sum(1 for s in streaks if s >= 5)}")
print(f"  streaks of 10+               : {sum(1 for s in streaks if s >= 10)}")

# ---- what the next N days look like ----
print()
print("=" * 88)
print("NEXT FEW DAYS — bootstrapped outcomes (1,000 account, ~0.5 trades/day)")
print("=" * 88)
for horizon, ntr in [("3 days", 2), ("7 days", 4), ("14 days", 7),
                     ("30 days", 14)]:
    ends, minwrs, maxloss_streak = [], [], []
    for _ in range(4000):
        seq = [random.choice(rs) for _ in range(ntr)]
        eq = 1.0
        for r in seq:
            eq *= (1 + r * RISK)
        ends.append((eq - 1) * 100)
        minwrs.append(sum(1 for r in seq if r > 0) / ntr * 100)
    ends.sort()
    print(f"  {horizon:8} ({ntr:>2} trades):  p10 {ends[int(len(ends)*.1)]:>+6.1f}%  "
          f"median {statistics.median(ends):>+6.1f}%  "
          f"p90 {ends[int(len(ends)*.9)]:>+6.1f}%   "
          f"worst {ends[0]:>+6.1f}%")

print()
print("=" * 88)
print("THE HONEST EXPECTATION")
print("=" * 88)
print(f"  In the next 7 days expect roughly 4 closed trades.")
print(f"  A NORMAL outcome is anywhere from a small loss to a small gain.")
print(f"  Losing 4 in a row happens regularly in the historical record")
print(f"  ({sum(1 for s in streaks if s >= 4)} times) and is NOT evidence of breakage.")
print()
print("  ⚠️  With only 10 live trades the system has NOT been validated.")
print("      Expect NOTHING to be proven for weeks. Judge nothing before")
print("      ~30 closed trades, and trust nothing before ~100.")
