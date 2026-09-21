"""MARKET MAKING — the one edge family we have never tested.

The idea, stated plainly: every trade we have ever placed PAID the spread.
A market maker EARNS it. That is a structurally different business.

The test is arithmetic, not opinion:
    profit per round trip  =  spread captured  -  fees paid
    if  spread < 2 x maker fee  ->  market making loses before any risk

Measure REAL spreads on the venues we can actually access, compare to the
maker fees we would actually pay, and see if the business exists for us.
"""
import json
import statistics
import time
import urllib.request

H = {"User-Agent": "Mozilla/5.0"}
SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
        "LINKUSDT", "AVAXUSDT", "NEARUSDT", "ARBUSDT"]


def get(u):
    r = urllib.request.Request(u, headers=H)
    return json.loads(urllib.request.urlopen(r, timeout=15).read())


# ---------------- Bybit order books (linear perps) ----------------
print("=" * 92)
print("BYBIT PERPS — real spread vs maker fee")
print("=" * 92)
print(f"{'symbol':10} {'bid':>12} {'ask':>12} {'spread bps':>11} "
      f"{'maker fee':>10} {'net/RT bps':>11} {'edge?':>7}")
bybit_rows = []
for s in SYMS:
    try:
        d = get(f"https://api.bybit.com/v5/market/orderbook?category=linear&symbol={s}&limit=1")
        r = d["result"]
        bid = float(r["b"][0][0])
        ask = float(r["a"][0][0])
        mid = (bid + ask) / 2
        sp_bps = (ask - bid) / mid * 10000
        maker_bps = 2.0                 # Bybit linear maker 0.02%
        net = sp_bps - 2 * maker_bps    # capture spread, pay maker both sides
        edge = "YES" if net > 0 else "no"
        bybit_rows.append((s, sp_bps, net))
        print(f"{s:10} {bid:>12,.4f} {ask:>12,.4f} {sp_bps:>11.2f} "
              f"{maker_bps:>9.2f}% {net:>11.2f} {edge:>7}")
    except Exception as e:
        print(f"{s:10} err {str(e)[:50]}")
    time.sleep(0.25)

# ---------------- Binance spot + futures ----------------
print()
print("=" * 92)
print("BINANCE — spot vs futures (maker fee differs a lot)")
print("=" * 92)
print(f"{'symbol':10} {'venue':>9} {'spread bps':>11} {'maker fee':>10} "
      f"{'net/RT bps':>11} {'edge?':>7}")
bn_rows = []
for s in SYMS[:8]:
    # spot
    try:
        d = get(f"https://api.binance.com/api/v3/ticker/bookTicker?symbol={s}")
        bid, ask = float(d["bidPrice"]), float(d["askPrice"])
        mid = (bid + ask) / 2
        sp = (ask - bid) / mid * 10000
        maker = 7.5            # 0.075% with BNB discount
        net = sp - 2 * maker
        bn_rows.append((s, "spot", sp, net))
        print(f"{s:10} {'spot':>9} {sp:>11.2f} {maker:>9.2f}% {net:>11.2f} "
              f"{'YES' if net>0 else 'no':>7}")
    except Exception as e:
        print(f"{s:10} spot err {str(e)[:40]}")
    # futures
    try:
        d = get(f"https://fapi.binance.com/fapi/v1/ticker/bookTicker?symbol={s}")
        bid, ask = float(d["bidPrice"]), float(d["askPrice"])
        mid = (bid + ask) / 2
        sp = (ask - bid) / mid * 10000
        maker = 2.0            # futures maker 0.02%
        net = sp - 2 * maker
        bn_rows.append((s, "futures", sp, net))
        print(f"{s:10} {'futures':>9} {sp:>11.2f} {maker:>9.2f}% {net:>11.2f} "
              f"{'YES' if net>0 else 'no':>7}")
    except Exception as e:
        print(f"{s:10} fut err {str(e)[:40]}")
    time.sleep(0.25)

print()
print("=" * 92)
print("SUMMARY")
print("=" * 92)
allsp = [r[1] for r in bybit_rows]
if allsp:
    print(f"  Bybit perps median spread : {statistics.median(allsp):.2f} bps")
    print(f"  Bybit maker round trip    : 4.00 bps")
    print(f"  -> median net             : "
          f"{statistics.median(allsp)-4:.2f} bps")
win_b = sum(1 for r in bybit_rows if r[2] > 0)
print(f"  Bybit symbols profitable  : {win_b}/{len(bybit_rows)}")
sp_spot = [r[2] for r in bn_rows if r[1] == "spot"]
sp_fut = [r[2] for r in bn_rows if r[1] == "futures"]
if sp_spot:
    print(f"  Binance spot   median spread: {statistics.median(sp_spot):.2f} bps "
          f"(maker RT 15.0 bps -> net "
          f"{statistics.median(sp_spot)-15:.2f})")
if sp_fut:
    print(f"  Binance futures median spread: {statistics.median(sp_fut):.2f} bps "
          f"(maker RT  4.0 bps -> net "
          f"{statistics.median(sp_fut)-4:.2f})")
print()
print("  NB: this is the GROSS structural test only. It ignores adverse")
print("      selection (you get filled precisely when price is moving away)")
print("      and inventory risk (you accumulate the losing side).")
