"""DO THE NEW EXTERNAL SIGNALS ACTUALLY IMPROVE OUR WIN RATE?

Live-verified working feeds (public-apis shortlist, DNS-checked):
  * Fear & Greed index   (api.alternative.me)   - full history since 2018
  * Binance open interest + funding             - real derivatives positioning
  * Coinpaprika global                          - mcap / BTC dominance
  * Fed Treasury, World Bank                    - macro

Test the one with usable history: FEAR & GREED as an entry filter.
Sentiment theory: extreme greed = crowded = poor forward returns,
extreme fear = capitulation = good forward returns. Does it hold for us?
"""
import importlib.util
import json
import statistics
import sys
import time
import urllib.request

# ---- fetch full Fear & Greed history ----
CACHE = "/opt/trading-agent/research/fng.json"
try:
    fng = json.load(open(CACHE))
except Exception:
    fng = {}
    for lim in ("0",):
        try:
            u = f"https://api.alternative.me/fng/?limit={lim}&format=json"
            r = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            d = json.loads(urllib.request.urlopen(r, timeout=25).read())
            for x in d["data"]:
                day = int(x["timestamp"]) // 86400
                fng[str(day)] = int(x["value"])
            break
        except Exception as e:
            print("fng fetch failed:", str(e)[:80])
            time.sleep(1)
    if fng:
        json.dump(fng, open(CACHE, "w"))
fng = {int(k): v for k, v in fng.items()}
print(f"Fear&Greed history: {len(fng)} days")
if fng:
    days_sorted = sorted(fng)
    print(f"  range: day {days_sorted[0]} -> {days_sorted[-1]}")

# ---- reuse the selectivity trade records ----
spec = importlib.util.spec_from_file_location(
    "sel", "/opt/trading-agent/research/11-selectivity.py")
sel = importlib.util.module_from_spec(spec)
sys.modules["sel"] = sel
spec.loader.exec_module(sel)
allt = sel.allt
DAYNUM = sel.days           # list of day numbers, index == trade["i"]


def fng_at(i):
    """F&G value on the calendar day the trade was entered."""
    if i < 0 or i >= len(DAYNUM):
        return None
    return fng.get(DAYNUM[i])


joined = [(t, fng_at(t["i"])) for t in allt]
joined = [(t, f) for t, f in joined if f is not None]
print(f"trades with F&G data: {len(joined)}/{len(allt)}")
if not joined:
    print("no overlap - F&G history may not reach back far enough")
    sys.exit()


def stats(grp):
    if not grp:
        return None
    wr = sum(1 for t in grp if t["won"]) / len(grp) * 100
    ex = statistics.mean([t["ret"] for t in grp]) * 100
    w = [t["ret"] for t in grp if t["won"]]
    l = [t["ret"] for t in grp if not t["won"]]
    pf = (statistics.mean(w) / abs(statistics.mean(l))) if w and l else 0
    return len(grp), wr, pf, ex


base = stats([t for t, _ in joined])
print()
print(f"BASELINE (all {base[0]} trades w/ F&G):  WR {base[1]:.1f}%  "
      f"payoff {base[2]:.2f}  exp {base[3]:+.3f}%")
print()
print("=" * 76)
print(f"{'Fear & Greed band':28} {'n':>5} {'WR':>7} {'payoff':>8} {'exp/tr':>9}")
print("=" * 76)
bands = [("EXTREME FEAR  (0-24)", 0, 24),
         ("FEAR          (25-44)", 25, 44),
         ("NEUTRAL       (45-55)", 45, 55),
         ("GREED         (56-74)", 56, 74),
         ("EXTREME GREED (75-100)", 75, 100)]
for lbl, lo, hi in bands:
    grp = [t for t, f in joined if lo <= f <= hi]
    s = stats(grp)
    if s:
        print(f"{lbl:28} {s[0]:>5} {s[1]:>6.1f}% {s[2]:>8.2f} {s[3]:>8.3f}%")

print()
print("--- as a FILTER (what would we gain/lose?) ---")
for lbl, fn in [
        ("skip EXTREME GREED (>74)",
         lambda f: f <= 74),
        ("skip GREED+ (>55)", lambda f: f <= 55),
        ("only FEAR or below (<45)", lambda f: f < 45),
        ("only NEUTRAL-FEAR (<56)", lambda f: f < 56),
]:
    grp = [t for t, f in joined if fn(f)]
    s = stats(grp)
    if s:
        kept = s[0] / len(joined) * 100
        print(f"  {lbl:28} n={s[0]:>4} WR {s[1]:>5.1f}%  payoff {s[2]:.2f}  "
              f"exp {s[3]:+.3f}%  kept {kept:.0f}%")

print()
print("--- correlation: does sentiment just proxy for trend? ---")
hi = [t for t, f in joined if f >= 56]
lo = [t for t, f in joined if f < 56]
if hi and lo:
    print(f"  greed+  : btc_mom avg {statistics.mean([t['btc_mom'] for t in hi])*100:+.2f}%")
    print(f"  fear-/nt: btc_mom avg {statistics.mean([t['btc_mom'] for t in lo])*100:+.2f}%")
    print("  (if these differ a lot, F&G is largely redundant with our BTC-mom gate)")
