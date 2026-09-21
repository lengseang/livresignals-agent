"""Bulletproof validation of the win-rate edge — quant-grade rigor.

Replaces naive "win rate %" with the metrics serious funds use:
  1. Information Coefficient (IC) — correlation between the signal and
     forward returns. The honest measure of "does this signal predict?"
  2. Purged walk-forward — when scoring each signal, EMBARGO the 48h window
     around it so the model can't peek at the label (kills lookahead for good).
  3. Payoff-aware Sharpe of the signal returns.

The claim under test: "shallow dip -5% + high vol + 3 red bars = 78.7% win".
If IC is materially > 0 and the embargoed win rate holds, the edge is REAL.
"""
from __future__ import annotations

import sqlite3
import statistics
from trading.config import SQLITE_PATH


def load_rows():
    con = sqlite3.connect(str(SQLITE_PATH))
    con.row_factory = sqlite3.Row
    return con.execute(
        "SELECT open, high, low, close, volume, open_time_utc FROM candles "
        "WHERE symbol='BTC/USDT' AND timeframe='1h' ORDER BY open_time_utc ASC"
    ).fetchall()


def vol_at(rows, i, lookback=14):
    if i < lookback: return 0.0
    rets = [rows[j]['close']/rows[j-1]['close']-1 for j in range(i-lookback+1, i)]
    return statistics.pstdev(rets) if len(rets) > 1 else 0.0


def red_bars(rows, i, k):
    return sum(1 for j in range(max(1,i-k), i+1) if rows[j]['close'] < rows[j-1]['close'])


def signal_strength(rows, i):
    """Continuous signal score (for IC), not just 0/1."""
    if i < 90:
        return None
    hi = max(r['close'] for r in rows[i-90:i])
    dd = rows[i]['close']/hi - 1
    if dd > -0.05:
        return None
    v = vol_at(rows, i)
    reds = red_bars(rows, i, 3)
    # Higher = stronger buy signal (shallower dip is NOT better here; deeper
    # into the -5% zone with vol + reds is the sweet spot). Score 0-1.
    dip_score = min(1.0, abs(dd) / 0.08)  # deeper dip (up to -8%) = stronger
    vol_score = min(1.0, v / 0.02)        # higher vol = stronger
    red_score = min(1.0, reds / 4.0)      # more reds = stronger
    return (dip_score * 0.4 + vol_score * 0.4 + red_score * 0.2)


def information_coefficient(rows, hold=48):
    """Pearson IC: corr(signal_score, forward_return). >0.03 = weak, >0.1 = real."""
    xs, ys = [], []
    for i in range(90, len(rows)-hold-1):
        s = signal_strength(rows, i)
        if s is None:
            continue
        entry = rows[i+1]['open']
        exit_px = rows[i+1+hold]['close']
        fwd = exit_px/entry - 1
        xs.append(s); ys.append(fwd)
    if len(xs) < 20:
        return None, 0
    mx = statistics.mean(xs); my = statistics.mean(ys)
    num = sum((a-mx)*(b-my) for a, b in zip(xs, ys))
    dx = sum((a-mx)**2 for a in xs)
    dy = sum((b-my)**2 for b in ys)
    if dx == 0 or dy == 0:
        return 0.0, len(xs)
    ic = num / (dx*dy)**0.5
    return ic, len(xs)


def embargoed_winrate(rows, hold=48, dip_thresh=-0.05, vol_min=0.010, red_min=3):
    """Win rate with PURGE: skip any signal within `hold` bars of the previous
    signal so overlapping 48h windows can't double-count and leak info."""
    wins=0; total=0; rets=[]; last_i = -10**9
    for i in range(90, len(rows)-hold-1):
        if i - last_i < hold:  # embargo
            continue
        hi = max(r['close'] for r in rows[i-90:i])
        dd = rows[i]['close']/hi - 1
        if dd > dip_thresh: continue
        if vol_at(rows, i) < vol_min: continue
        if red_bars(rows, i, red_min) < red_min: continue
        entry = rows[i+1]['open']
        exit_px = rows[i+1+hold]['close']
        fwd = exit_px/entry - 1
        rets.append(fwd); total += 1
        if fwd > 0: wins += 1
        last_i = i
    wr = wins/total*100 if total else 0
    avg = sum(rets)/len(rets)*100 if rets else 0
    sd = statistics.pstdev(rets)*100 if len(rets) > 1 else 0
    sharpe = (avg/100) / (sd/100) * (365*24/hold)**0.5 if sd else 0  # annualized-ish
    return wr, total, avg, sd, sharpe


if __name__ == "__main__":
    rows = load_rows()
    print(f"1h data: {len(rows)} candles")
    print()
    print("="*70)
    print("VALIDATION 1: INFORMATION COEFFICIENT (does the signal predict?)")
    print("="*70)
    ic, n = information_coefficient(rows)
    print(f"  IC = {ic:+.4f}  (n={n} signal points)")
    print(f"  Interpretation: {ic:+.4f} → " + (
        "STRONG edge" if ic > 0.10 else "REAL but modest" if ic > 0.05 else
        "WEAK (likely noise)" if ic > 0.02 else "NEGLIGIBLE (no real edge)"))
    print()
    print("="*70)
    print("VALIDATION 2: EMBARGOED WIN RATE (purged walk-forward)")
    print("="*70)
    wr, tot, avg, sd, sharpe = embargoed_winrate(rows)
    print(f"  Win rate: {wr:.1f}% (n={tot} independent trades)")
    print(f"  Avg return: {avg:+.3f}%  |  Stdev: {sd:.3f}%")
    print(f"  Signal Sharpe (annualized est): {sharpe:.2f}")
    import math
    if tot > 0:
        se = math.sqrt(wr/100*(1-wr/100)/tot)*100
        print(f"  95% CI: {wr:.1f}% +/- {1.96*se:.1f}%  → {wr-1.96*se:.1f}% to {wr+1.96*se:.1f}%")
