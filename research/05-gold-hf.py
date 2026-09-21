"""HIGH-FREQUENCY GOLD TEST — does 'fire every moment' actually work?

Owner's idea: fire lots of continuous trades on gold, catching small moves,
instead of waiting for a big signal.

Tests that EXACT idea on real Bybit gold data at 1m / 5m / 15m with REAL fees
(0.055% taker each side = 0.11% round trip). Strategies: momentum, mean-revert,
and random (as a control).

If firing constantly worked, we'd see positive expectancy after fees.
"""
import sys

sys.path.insert(0, "/opt/trading-agent/scripts")
from bybit_demo import public      # noqa: E402

FEE = 0.0011        # 0.11% round trip (taker both sides)


def klines(symbol, interval, limit=1000):
    r = public("/v5/market/kline", {"category": "linear", "symbol": symbol,
                                    "interval": interval, "limit": str(limit)})
    lst = r.get("result", {}).get("list", [])
    if not lst:
        return []
    return [float(x[4]) for x in reversed(lst)]     # close, ascending


def run(closes, mode, hold):
    """mode: momentum | reversion | random. hold: bars to hold."""
    import random
    random.seed(42)
    trades = 0
    gross = 0.0
    i = 20
    while i < len(closes) - hold:
        if mode == "momentum":
            fire = closes[i] > closes[i - 1]
        elif mode == "reversion":
            fire = closes[i] < closes[i - 1]
        else:
            fire = random.random() < 0.5
        if fire:
            r = closes[i + hold] / closes[i] - 1
            gross += r
            trades += 1
            i += hold
        else:
            i += 1
    if not trades:
        return None
    net = gross - trades * FEE
    return trades, gross / trades * 100, net / trades * 100, net * 100


print(f"REAL GOLD (XAUUSDT, Bybit). Fee = {FEE*100:.2f}% round trip.\n")
for interval, label in [("1", "1 minute"), ("5", "5 minute"), ("15", "15 minute")]:
    cl = klines("XAUUSDT", interval)
    if len(cl) < 100:
        print(f"{label}: insufficient data ({len(cl)})")
        continue
    print(f"--- {label} bars ({len(cl)} bars) ---")
    print(f"{'strategy':12} {'hold':>5} {'trades':>7} {'gross/tr':>9} "
          f"{'fee/tr':>8} {'net/tr':>9} {'total net':>10}")
    for mode in ["momentum", "reversion", "random"]:
        for hold in [1, 3, 5]:
            r = run(cl, mode, hold)
            if not r:
                continue
            t, g, n, tot = r
            flag = "✅" if n > 0 else "❌"
            print(f"{mode:12} {hold:>5} {t:>7} {g:>8.4f}% {-FEE*100:>7.3f}% "
                  f"{n:>8.4f}% {tot:>9.2f}% {flag}")
    print()
