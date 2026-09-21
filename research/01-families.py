"""HUNT FOR A REAL EDGE — untested strategy families.

We already ruled out: time-series trend (~break-even), dip-buying (random),
scalping (fee death). Unexplored and documented:
  A. CROSS-SECTIONAL MOMENTUM  (rank coins, long winners)  <-- best-documented
  B. VOLATILITY SQUEEZE BREAKOUT (low vol -> expansion)
  C. MEAN REVERSION at extremes (buy panic, sell euphoria)
  D. TIME-SERIES TREND (baseline, known break-even)

Method that matters (learned the hard way): simulate the ACTUAL SHARED
ACCOUNT. Never average per-coin curves - that inflates results.
"""
import json
import time
import urllib.request
from pathlib import Path

CACHE = Path("/tmp/edge_data.json")
FEE = 0.00144          # 0.144% round trip (BNB-discounted spot)
COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "LINK", "DOGE", "AVAX",
         "NEAR", "ARB", "DOT", "LTC", "ATOM", "MATIC", "UNI", "APT", "SUI",
         "INJ", "OP", "TIA", "SEI", "FIL", "AAVE", "ETC"]


def fetch_daily(sym, limit=1000):
    url = (f"https://api.binance.com/api/v3/klines?symbol={sym}USDT"
           f"&interval=1d&limit={limit}")
    for _ in range(3):
        try:
            r = urllib.request.urlopen(url, timeout=20)
            d = json.loads(r.read())
            return {int(k[0] // 86400000): float(k[4]) for k in d}
        except Exception:
            time.sleep(1)
    return {}


if CACHE.exists():
    data = {k: {int(ts): v for ts, v in d.items()}
            for k, d in json.loads(CACHE.read_text()).items()}
else:
    data = {}
    for c in COINS:
        d = fetch_daily(c)
        if len(d) > 500:
            data[c] = d
            print(f"  {c}: {len(d)} days")
    CACHE.write_text(json.dumps({k: {str(t): v for t, v in d.items()}
                                 for k, d in data.items()}))
print(f"\nloaded {len(data)} coins")

# drop coins with truncated history (e.g. delisted/renamed) - they destroy
# the common intersection window and leave too little data to test on
latest = max(max(d) for d in data.values())
earliest = max(min(d) for d in data.values())
good = {c: d for c, d in data.items()
        if max(d) >= latest - 3 and min(d) <= earliest + 3}
dropped = sorted(set(data) - set(good))
if dropped:
    print(f"dropped (truncated history): {dropped}")
data = good

# align on common days
all_days = sorted(set.intersection(*[set(d) for d in data.values()]))
print(f"aligned: {len(all_days)} days ({all_days[0]} -> {all_days[-1]})")
px = {c: [data[c][d] for d in all_days] for c in data}
coins = sorted(data)


def bt(start, end, mode, topn=3, **kw):
    """Shared-account backtest. Returns (return%, trades, maxDD%)."""
    cash, pos, peak, mdd, trades = 1.0, {}, 1.0, 0.0, 0
    warm = 60 if mode != "xs_mom" else 31
    for i in range(max(start, warm), end):
        # ---- exits ----
        for c in list(pos):
            p = pos[c]
            held = i - p["i"]
            px_now = px[c][i]
            reason = None
            if mode == "xs_mom":
                if held >= kw.get("hold", 7):
                    reason = "rebal"
            elif mode == "squeeze":
                if held >= kw.get("hold", 10):
                    reason = "time"
                elif px_now < p["entry"] * 0.92:
                    reason = "stop"
            elif mode == "reversion":
                if px_now >= p["entry"] * 1.04:
                    reason = "tp"
                elif px_now <= p["entry"] * 0.95:
                    reason = "sl"
                elif held >= 10:
                    reason = "time"
            if reason:
                cash += p["size"] * (px_now / p["entry"]) * (1 - FEE / 2)
                del pos[c]
                trades += 1
        # ---- entries ----
        if mode == "xs_mom":
            if not pos:      # rebalance into fresh top-N
                scores = []
                for c in coins:
                    if i >= 30 and px[c][i - 30] > 0:
                        scores.append((px[c][i] / px[c][i - 30] - 1, c))
                scores.sort(reverse=True)
                picks = [c for _, c in scores[:topn]]
                if picks and cash > 0.2:
                    per = cash / len(picks)
                    for c in picks:
                        pos[c] = {"entry": px[c][i], "i": i, "size": per}
                    cash -= per * len(picks)
                    trades += len(picks)
        else:
            for c in coins:
                if len(pos) >= topn or c in pos or cash < 0.2:
                    continue
                w = px[c][max(0, i - 20):i]
                if len(w) < 20:
                    continue
                mean = sum(w) / len(w)
                sd = (sum((v - mean) ** 2 for v in w) / len(w)) ** 0.5
                w50 = px[c][max(0, i - 50):i]
                long_up = (len(w50) >= 50 and px[c][i] > sum(w50) / len(w50))
                if mode == "squeeze":
                    # tight range then break above
                    rng = (max(w) - min(w)) / mean
                    if rng < 0.15 and px[c][i] >= max(w) and px[c][i] > mean:
                        sz = cash / topn
                        pos[c] = {"entry": px[c][i], "i": i, "size": sz}
                        cash -= sz
                        trades += 1
                elif mode == "reversion":
                    # buy a panic dip, but only inside a longer-term uptrend
                    if px[c][i] < mean - 1.5 * sd and long_up:
                        sz = cash / topn
                        pos[c] = {"entry": px[c][i], "i": i, "size": sz}
                        cash -= sz
                        trades += 1
        # ---- mark ----
        eq = cash + sum(p["size"] * (px[c][i] / p["entry"]) for c, p in pos.items())
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    # liquidate
    eq = cash + sum(p["size"] * (px[c][end - 1] / p["entry"]) * (1 - FEE / 2)
                    for c, p in pos.items())
    return (eq - 1) * 100, trades, mdd * 100


n = len(all_days)
mid = int(n * 0.6)
print(f"\nIS: days 0-{mid}   OOS: days {mid}-{n}")
print("=" * 74)
print(f"{'strategy':28} {'IS ret':>9} {'OOS ret':>9} {'OOS DD':>8} {'tr':>6}")
print("=" * 74)
for name, mode, kw in [
        ("A. xs-momentum top3/30d", "xs_mom", {"topn": 3, "hold": 7}),
        ("A2. xs-momentum top5/30d", "xs_mom", {"topn": 5, "hold": 14}),
        ("B. vol squeeze breakout", "squeeze", {"topn": 3, "hold": 10}),
        ("C. mean reversion -1.5sd", "reversion", {"topn": 3}),
]:
    try:
        is_r, is_t, _ = bt(0, mid, mode, **kw)
        oos_r, oos_t, oos_dd = bt(mid, n, mode, **kw)
        flag = "OK" if oos_r > 0 else "  "
        print(f"{name:28} {is_r:>8.1f}% {oos_r:>8.1f}% {oos_dd:>7.1f}% {oos_t:>6} {flag}")
    except Exception as e:
        print(f"{name:28} ERROR {str(e)[:40]}")
