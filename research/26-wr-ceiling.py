"""THE WIN-RATE CEILING — how high can accuracy ACTUALLY go, and at what price?

The user asks when we reach 60-70%. This measures it directly instead of
arguing about it.

Two knobs control win rate mechanically:
  1. TARGET DISTANCE (TP as a fraction of R). Tighter target = more hits.
  2. EXIT STYLE. The mom5 exit closes trades on momentum decay, which is a
     LOWER win-rate exit than a target hit. Removing it should raise WR.

So the WR-maximising configuration is: tightest target + no adaptive exit
(pure stop/target/hold). That is the ceiling. Measure it, then read the
expectancy at that ceiling.

Also reported: the breakeven WR at each point, so the cost is explicit.
"""
import json
import statistics

FEE = 0.00144
HOLD = 21
SMA_TREND = 50
SMA_REGIME = 150

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


def run(tp_r, stop_mult, adaptive_exit):
    """returns list of R-multiples"""
    out = []
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
            risk = min(stop_mult * vol, 0.35)
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
                if adaptive_exit and j >= 6 and (a[j] / a[j - 5] - 1) * side < 0:
                    r = move - FEE
                    break
            if r is None:
                jx = min(i + HOLD, n - 1)
                r = (a[jx] / entry - 1) * side - FEE
            out.append(r / risk)
    return out


def summ(rs):
    w = [x for x in rs if x > 0]
    l = [x for x in rs if x <= 0]
    wr = len(w) / len(rs) * 100
    payoff = statistics.mean(w) / abs(statistics.mean(l)) if w and l else 0
    return wr, payoff, statistics.mean(rs), 100 / (1 + payoff) if payoff else 0


print("=" * 100)
print("WIN-RATE CEILING — tightest targets, best stop, with/without the adaptive exit")
print("=" * 100)
print(f"{'TP':>7} {'stop':>6} {'adaptive':>9} {'WR':>7} {'payoff':>7} "
      f"{'exp/trade':>11} {'breakeven':>10} {'cost of the WR':>26}")
print("-" * 100)

best_wr_profitable = None
highest_wr = (0, None)
for tp in (0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50, 0.75, 1.0):
    for sm in (1.5, 2.5, 3.5):
        for ad in (False, True):
            rs = run(tp, sm, ad)
            wr, pay, ex, be = summ(rs)
            if wr > highest_wr[0]:
                highest_wr = (wr, (tp, sm, ad, pay, ex))
            if ex > 0 and (best_wr_profitable is None or
                           wr > best_wr_profitable[0]):
                best_wr_profitable = (wr, tp, sm, ad, pay, ex)
            if ad:
                continue          # print only the WR-maximising variant
            flag = f"{ex*100:+.2f}% of risk" if ex > 0 else "LOSES"
            print(f"{tp:>7.2f} {sm:>6.1f} {'no':>9} {wr:>6.1f}% {pay:>7.2f} "
                  f"{ex:>+10.3f}R {be:>9.1f}% {flag:>26}")

print()
print("=" * 100)
print("THE ANSWER")
print("=" * 100)
wr, meta = highest_wr
tp, sm, ad, pay, ex = meta
print(f"  HIGHEST WIN RATE ACHIEVABLE: {wr:.1f}%")
print(f"    config: TP {tp:g}R, stop {sm:g}xATR, adaptive exit {'yes' if ad else 'no'}")
print(f"    payoff {pay:.2f} → breakeven WR would need to be "
      f"{100/(1+pay):.1f}%")
print(f"    actual expectancy: {ex:+.3f}R per trade  → "
      f"{'PROFITABLE' if ex > 0 else '*** LOSES MONEY ***'}")
print()
if best_wr_profitable:
    bw, btp, bsm, bad, bpay, bex = best_wr_profitable
    print(f"  HIGHEST PROFITABLE WIN RATE: {bw:.1f}%")
    print(f"    config: TP {btp:g}R, stop {bsm:g}xATR, "
          f"adaptive exit {'yes' if bad else 'no'}")
    print(f"    payoff {bpay:.2f}, expectancy {bex:+.3f}R")
else:
    print("  HIGHEST PROFITABLE WIN RATE: none found")
print()
print("  RATIO CHECK: for every 1pp of extra win rate, how much expectancy")
print("  is given up?")
rows = []
for tp in (0.10, 0.20, 0.30, 0.50, 1.0, 2.0, 99.0):
    rs = run(tp, 1.5, True)
    wr, pay, ex, _ = summ(rs)
    rows.append((wr, ex))
for a, b in zip(rows, rows[1:]):
    dw, de = b[0] - a[0], b[1] - a[1]
    if dw:
        print(f"    WR {a[0]:>5.1f}%→{b[0]:>5.1f}%  ({dw:+.1f}pp)   "
              f"expectancy {a[1]:+.3f}→{b[1]:+.3f}R  "
              f"({de:+.3f}R  = {de/dw*100:+.1f} per pp)")
