"""SUPERVISOR DIAGNOSTIC — why do 40% of trades stop out?

HYPOTHESIS: the stop distance is mis-scaled for the holding period.

  Current: stop = 2.5 x ATR(14 daily)        = ~10% on crypto
  Expected 21-day move  = ATR x sqrt(H)      = 4% x 4.58 = ~18%

  So the stop sits at 0.54x the random-walk expectation for the horizon.
  A coin that merely wanders will hit it. That is not risk control, that is
  a coin flip wearing a risk-management costume.

THE FIX TO TEST: scale the stop with sqrt(holding period), so the stop is
proportionate to the noise it must survive.

  stop = k x ATR x sqrt(HOLD)      or a fixed multiple of HOLD-day vol

Also tests exit-rule alternatives, since 'trend' exits (SMA50 cross) are
another 18% of outcomes and lag by construction.

SCORING: not aggregate return. We score CONSISTENCY across the 3 time
segments, because instability (32%->46% WR swing) is the actual disease.
"""
import json
import math
import statistics

FEE = 0.00144
HOLD = 21
SMA_TREND = 50
SMA_REGIME = 150
TP_R = 2.0
STOP_ATR = 2.5

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


def breadth_at(i):
    up = tot = 0
    for c in coins:
        s = sma(px[c], i, SMA_TREND)
        if s:
            tot += 1
            up += px[c][i] > s
    return up / tot if tot else 0.5


def simulate(i, c, side, entry, vol, stop_mode, exit_mode):
    """returns (ret, why)"""
    a = px[c]
    if stop_mode == "atr":
        risk = min(STOP_ATR * vol, 0.25)
    elif stop_mode == "atr_sqrt":
        risk = min(STOP_ATR * vol * math.sqrt(HOLD), 0.35)
    elif stop_mode == "atr_sqrt_half":
        risk = min(STOP_ATR * vol * (HOLD ** 0.25), 0.30)
    elif stop_mode == "atr_wide":
        risk = min(3.5 * vol * math.sqrt(HOLD), 0.40)
    elif stop_mode == "vol21":
        # realised 21-day volatility - the noise the trade must survive
        if i < 22:
            return None, None
        rets = [a[j] / a[j - 1] - 1 for j in range(i - 21, i)]
        s21 = statistics.pstdev(rets)
        risk = min(2.0 * s21, 0.35)
    else:
        return None, None

    stop = entry * (1 - risk) if side == 1 else entry * (1 + risk)
    tp = entry * (1 + TP_R * risk) if side == 1 else entry * (1 - TP_R * risk)

    for j in range(i + 1, min(i + HOLD + 1, n)):
        move = (a[j] / entry - 1) * side
        hit_stop = (a[j] <= stop) if side == 1 else (a[j] >= stop)
        if hit_stop:
            return -risk - FEE, "stop"
        if move >= TP_R * risk:
            return TP_R * risk - FEE, "tp"
        if exit_mode == "sma50":
            s = sma(a, j, SMA_TREND)
            if s and ((side == 1 and a[j] < s) or (side == -1 and a[j] > s)):
                return move - FEE, "trend"
        elif exit_mode == "mom5":
            if j >= 6:
                if (a[j] / a[j - 5] - 1) * side < 0:
                    return move - FEE, "mom-decay"
        elif exit_mode == "sma50or_mom":
            s = sma(a, j, SMA_TREND)
            if s and ((side == 1 and a[j] < s) or (side == -1 and a[j] > s)):
                return move - FEE, "trend"
            if j >= 6 and (a[j] / a[j - 5] - 1) * side < 0:
                return move - FEE, "mom-decay"
    jx = min(i + HOLD, n - 1)
    return (a[jx] / entry - 1) * side - FEE, "max-hold"


SEG = n // 3
results = {}


def run(stop_mode, exit_mode):
    per_seg = [[], [], []]
    whys = {}
    for i in range(210, n - 2):
        s200 = sma(px["BTC"], i, SMA_REGIME)
        bear = bool(s200 and px["BTC"][i] < s200)
        for c in coins:
            a = px[c]
            s50 = sma(a, i, SMA_TREND)
            if not s50:
                continue
            side = -1 if bear else 1
            if side == 1 and a[i] <= s50:
                continue
            if side == -1 and a[i] >= s50:
                continue
            vol = atr(a, i) or 0.03
            r, why = simulate(i, c, side, a[i], vol, stop_mode, exit_mode)
            if r is None:
                continue
            per_seg[min(2, i // SEG)].append(r)
            whys[why] = whys.get(why, 0) + 1
    key = f"{stop_mode:14} + {exit_mode:13}"
    results[key] = (per_seg, whys)
    return per_seg, whys


def show(key):
    per_seg, whys = results[key]
    wrs, exps = [], []
    for seg in per_seg:
        if not seg:
            wrs.append(0)
            exps.append(0)
            continue
        wrs.append(sum(1 for r in seg if r > 0) / len(seg) * 100)
        exps.append(statistics.mean(seg) * 100)
    allr = [r for s in per_seg for r in s]
    wr = sum(1 for r in allr if r > 0) / len(allr) * 100
    exp = statistics.mean(allr) * 100
    stops = whys.get("stop", 0) / max(1, len(allr)) * 100
    spread = max(wrs) - min(wrs)
    print(f"{key} n={len(allr):>5} WR {wr:>5.1f}% exp {exp:>+7.3f}% "
          f"stop% {stops:>4.1f} | segs {wrs[0]:>4.1f}/{wrs[1]:>4.1f}/"
          f"{wrs[2]:>4.1f} spread {spread:>4.1f}")


print("=" * 100)
print("STOP SCALING TEST — is the stop mis-scaled for a 21-day horizon?")
print("=" * 100)
for sm in ("atr", "atr_sqrt_half", "atr_sqrt", "atr_wide", "vol21"):
    run(sm, "sma50")
    show(f"{sm:14} + {'sma50':13}")

print()
print("=" * 100)
print("EXIT RULE TEST — on the best stop scaling")
print("=" * 100)
for ex in ("sma50", "mom5", "sma50or_mom"):
    run("atr_sqrt", ex)
    show(f"{'atr_sqrt':14} + {ex:13}")

print()
print("=" * 100)
print("EXIT MIX COMPARISON (current vs candidates)")
print("=" * 100)
for k in results:
    per_seg, whys = results[k]
    tot = sum(whys.values()) or 1
    mix = " ".join(f"{w}:{whys.get(w,0)*100//tot}%" for w in
                   ("tp", "stop", "trend", "mom-decay", "max-hold")
                   if whys.get(w))
    print(f"  {k:30} {mix}")

json.dump({k: {"per_seg": [len(s) for s in v[0]]} for k, v in results.items()},
          open("/tmp/supervisor-run.json", "w"))
