#!/usr/bin/env python3
"""BACKTEST: does an OKX-OI veto improve the daily trend book?

Mirrors trend_follower.py: shared book, MAX_POS=3 slots, entry = coin>SMA50 AND
BTC>SMA200, candidates ranked by 30d momentum, fee 0.1%/side. Exit = coin drops
below its SMA50 (or -15% hard stop). No lookahead. IS/OOS 60/40, fold signs
printed BEFORE totals, per-trade decomposition (best-2 share).

VETO RULE (from external-signals-and-tool-vetting.md §4):
    skip an entry when BTC PRICE ROSE over the lookback while BTC OPEN INTEREST
    FELL — a short-covering squeeze, prone to fading.

Days are aligned on UTC date across Binance klines and OKX OI.
"""
from __future__ import annotations
import json, sys
from pathlib import Path

FEE = 0.001          # 0.1% per side
SMA_N = 50
MAX_POS = 3
LOOKBACK = 3         # OI/price lookback for the squeeze test
sys.path.insert(0, "/opt/trading-agent/src")
from trading.signals.okx_oi import fetch_history, to_daily_series   # noqa: E402


def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def load_daily():
    raw = json.load(open("/tmp/daily_closes.json"))
    out = {}
    for s, rows in raw.items():
        out[s] = {r[0] // 86400000 * 86400000: r[1] for r in rows}   # day-keyed
    return out


def load_oi():
    rows = fetch_history("BTC-USDT-SWAP", "1D", pages=8)
    dates, oi = to_daily_series(rows)          # yyyy-mm-dd
    import datetime as dt
    return {int(dt.datetime.strptime(d, "%Y-%m-%d").replace(
        tzinfo=dt.timezone.utc).timestamp()) * 1000: v for d, v in zip(dates, oi)}


def align(closes, oi_by_day):
    """Common sorted day-keys present in every symbol AND the OI series."""
    days = set(closes["BTCUSDT"]) & set(oi_by_day)
    for s in closes:
        days &= set(closes[s])
    return sorted(days)


def squeeze_at(days, i, btc_oi):
    """True when BTC price ROSE but BTC OI FELL over LOOKBACK days ending at i."""
    if i - LOOKBACK < 0:
        return False
    d0, d1 = days[i - LOOKBACK], days[i]
    px0, px1 = None, None
    # btc price handled by caller via closes; here only OI. Caller merges.
    o0, o1 = btc_oi.get(d0), btc_oi.get(d1)
    if not o0 or not o1:
        return False
    oi_fell = o1 < o0
    return oi_fell


def run(closes, oi_by_day, days, use_veto):
    syms = list(closes.keys())
    eq = 1.0
    held = set()
    trades = []          # (day_index, symbol, pnl_frac)
    entry_px = {}
    entry_i = {}
    for i in range(205, len(days) - 1):
        d = days[i]
        btc = [closes["BTCUSDT"][x] for x in days[:i + 1]]
        btc_ok = sma(btc, 200) is not None and btc[-1] > sma(btc, 200)
        # ---- decide target holdings (recompute each day) ----
        cands = []
        for s in syms:
            cs = [closes[s][x] for x in days[:i + 1]]
            if len(cs) < 60:
                continue
            if btc_ok and cs[-1] > sma(cs, SMA_N):
                mom = cs[-1] / cs[-31] - 1 if len(cs) > 31 else 0.0
                cands.append((mom, s))
        cands.sort(reverse=True)
        target = [s for _, s in cands[:MAX_POS]]
        if use_veto and squeeze_at(days, i, oi_by_day):
            # squeeze day: refuse NEW entries (keep existing holds)
            target = [s for s in target if s in held]
        target = set(target)
        # ---- close positions no longer wanted ----
        for s in held - target:
            if s in entry_px:
                pnl = closes[s][days[i + 1]] / entry_px[s] - 1 - 2 * FEE
                trades.append((i, s, pnl))
        # ---- fee on the changed notional weight ----
        ch = len(held ^ target)
        if ch and (held | target):
            eq *= (1 - FEE / max(len(target), 1)) ** ch
        # ---- mark to market ----
        if target:
            pr = sum(closes[s][days[i + 1]] / closes[s][days[i]] - 1
                     for s in target) / len(target)
            eq *= (1 + pr)
        for s in target - held:
            entry_px[s] = closes[s][days[i + 1]]
            entry_i[s] = i
        held = target
    return eq, trades


def bench(closes, days):
    a, b = closes["BTCUSDT"][days[205]], closes["BTCUSDT"][days[-1]]
    return b / a - 1


def report(name, eq, trades, days, n_days):
    wr = (sum(1 for t in trades if t[2] > 0) / len(trades)) if trades else 0
    tot = sum(t[2] for t in trades)
    ordered = sorted((t[2] for t in trades), reverse=True)
    best2 = sum(ordered[:2]) if ordered else 0
    print(f"{name:22} ret={eq-1:+7.1%}  trades={len(trades):>3}  WR={wr:5.1%}  "
          f"sum={tot:+7.1%}  best2share={best2/tot if tot else 0:5.0%}")
    return eq, trades


def main():
    closes = load_daily()
    oi = load_oi()
    days = align(closes, oi)
    print(f"aligned days: {len(days)}  {days[0]}..{days[-1]}")
    split = 205 + int((len(days) - 205) * 0.6)
    # fold boundaries across the post-warmup region
    folds = [205 + int((len(days) - 205) * k / 6) for k in range(7)]

    for use_veto in (False, True):
        eq, trades = run(closes, oi, days, use_veto)
        label = "OI-VETO" if use_veto else "baseline"
        report(label, eq, trades, days, len(days))
        # IS/OOS
        is_t = [t for t in trades if t[0] < split]
        oos_t = [t for t in trades if t[0] >= split]
        isr = sum(t[2] for t in is_t); oosr = sum(t[2] for t in oos_t)
        # folds
        fsigns = []
        for a, b in zip(folds, folds[1:]):
            ft = [t for t in trades if a <= t[0] < b]
            fsigns.append(sum(t[2] for t in ft))
        print(f"   IS sum={isr:+7.1%} (n={len(is_t)})   OOS sum={oosr:+7.1%} (n={len(oos_t)})")
        print(f"   fold sums: {[f'{x:+.1%}' for x in fsigns]}  "
              f"pos={sum(1 for x in fsigns if x>0)}/{len(fsigns)}")
    print(f"\nBTC buy&hold (same window): {bench(closes, days):+.1%}")


if __name__ == "__main__":
    main()

def decompose():
    closes=load_daily(); oi=load_oi(); days=align(closes,oi)
    import datetime as dt
    for use_veto in (False,True):
        eq,trades=run(closes,oi,days,use_veto)
        ordered=sorted(trades,key=lambda t:-t[2])
        print(f"\n=== {'OI-VETO' if use_veto else 'baseline'} top 5 trades ===")
        for i,s,pnl in ordered[:5]:
            d=dt.datetime.utcfromtimestamp(days[i]/1000).date()
            print(f"  {d} {s:10} {pnl:+8.1%}")
        rest=sum(t[2] for t in ordered[2:])
        print(f"  best2={ordered[0][2]+ordered[1][2]:+.1%}  all-others={rest:+.2%}  n={len(trades)}")
