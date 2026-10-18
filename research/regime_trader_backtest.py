#!/usr/bin/env python3
"""Backtest the SHIPPED regime_trader.main() logic on stored candles.

Why this exists
---------------
`regime_trader.main()` derives its BULL/BEAR label from `btc[-1] < sma(btc,
SMA_REGIME)`. `SMA_REGIME` is 200 because `champion.json` sets `sma_r: 200`,
and `champion.py`'s variant grid regenerates that file every night. On the
LIVE daily series that matches its docstring ("BTC > SMA200"). But the
backtest that earned the edge (+29.7% full / +40.8% OOS, DD -21.9%) is not
reproducible from the shipped file without a simulation, so this script IS
that simulation: it replays `main()`'s entry / exit / gate rules bar by bar on
the candles already in `data/trading.db`.

It deliberately mirrors `main()` rather than importing it (main() talks to
Bybit and Telegram, and reads live state). `test_regime_trader_sim_matches_main`
pins the pieces that must not drift: SMAs, ATR%, risk sizing, exit precedence
and the gate.

Also measures the SECOND problem this surfaced: the 15m dip opener that the
paper book is learning on can open LONG while this trader is, on the same day,
SHORT the same coin (opposite calls from the same regime label).

Pure stdlib + sqlite (read-only). No network, no orders.

Run:  venv/bin/python research/regime_trader_backtest.py [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "trading.db"

# ---- shipped constants from scripts/regime_trader.py ---------------------
SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT",
        "DOGEUSDT", "AVAXUSDT", "NEARUSDT", "DOTUSDT", "ATOMUSDT", "LTCUSDT"]
GOLD_SYM = "XAUTUSDT"
RESERVE_GOLD = False        # XAUT is not in the sqlite universe; set False
BOOK = 1000.0
TOPN = 3
HOLD = 21
STOP = 0.10
RISK = 0.01
TP_R = 99.0                 # "no take-profit" sentinel (champion 2026-09-15)
STOP_ATR = 1.5
EXIT_MODE = "mom5"
SMA_TREND = 50
GATE_BTC_MOM = 0.05
GATE_BREADTH = 0.12
USE_GATE = True

COST_RT = 0.0015555         # measured paper round trip, per side ~half


def sma(v, n, i):
    """SMA of v[i-n+1 .. i] (inclusive), None if not enough history."""
    if i + 1 < n:
        return None
    return sum(v[i - n + 1:i + 1]) / n


def atr_frac(hi, lo, cl, n, i):
    if i < n:
        return None
    trs = [max(hi[j] - lo[j], abs(hi[j] - cl[j - 1]), abs(lo[j] - cl[j - 1]))
           for j in range(i - n + 1, i + 1)]
    return (sum(trs) / len(trs)) / cl[i]


def load(conn, tf="1d"):
    out = {}
    for s in SYMS:
        rows = conn.execute(
            "select open_time_utc, high, low, close from candles "
            "where symbol=? and timeframe=? order by open_time_utc",
            (s.replace("USDT", "/USDT"), tf)).fetchall()
        if rows:
            out[s] = rows
    return out


def run(data, sma_regime, cost=True, start=None, end=None, btc_bench=True):
    """Replay main(). Returns (trades, book_curve) — book is $1000 start."""
    btc = data.get("BTCUSDT") or []
    book = BOOK
    cur = 0.0
    pos = []          # open positions
    trades = []
    equity = []
    for i in range(len(btc)):
        ts, _, _, bpx = btc[i]
        if start and ts < start:
            continue
        if end and ts > end:
            break
        b = sma([r[3] for r in btc], sma_regime, i)
        if b is None:
            continue
        bear = bpx < b
        # ---- exits (evaluated first, exactly like main()) ----
        for p in list(pos):
            rows = data[p["sym"]]
            if i >= len(rows) or rows[i][0] != ts:
                continue
            px = rows[i][3]
            side = p["side"]
            held = i - p["i"]
            move = (px / p["entry"] - 1) * side
            risk_pct = p["risk_pct"]
            why = None
            if move >= TP_R * risk_pct:
                why = f"TP {TP_R:g}R"
            elif move <= -risk_pct:
                why = "hard-stop"
            elif EXIT_MODE == "mom5":
                m5 = rows[i][3] / rows[i - 5][3] - 1 if i > 5 else 0.0
                if m5 * side < 0:
                    why = "mom-decay"
                elif held >= HOLD:
                    why = "max-hold"
            else:
                s50 = sma([r[3] for r in rows], SMA_TREND, i)
                if (side == 1 and px < (s50 or 0)) or (side == -1 and s50 and px > s50):
                    why = "trend-broke"
                elif held >= HOLD:
                    why = "max-hold"
            if why:
                notional = p["qty"] * p["entry"]
                gross = (px - p["entry"]) * p["qty"] * side
                fee = notional * COST_RT if cost else 0.0
                book += gross - fee
                trades.append({"sym": p["sym"], "side": side, "entry": p["entry"],
                               "exit": px, "ret": round(move, 5),
                               "pnl": round(gross - fee, 4), "why": why,
                               "entry_i": p["i"], "exit_i": i})
                pos.remove(p)
        # ---- entries ----
        if len(pos) < TOPN:
            btc_mom = bpx / btc[i - 30][3] - 1 if i >= 30 else 0.0
            above = tot = 0
            for s in SYMS:
                rows = data.get(s) or []
                if i >= len(rows) or rows[i][0] != ts:
                    continue
                s50 = sma([r[3] for r in rows], SMA_TREND, i)
                if s50 is None:
                    continue
                tot += 1
                above += rows[i][3] > s50
            bread = above / tot if tot else 0.0
            gate_ok = (not USE_GATE) or (btc_mom > GATE_BTC_MOM and bread > GATE_BREADTH)
            cands = []
            for s in SYMS:
                rows = data.get(s) or []
                if i >= len(rows) or rows[i][0] != ts:
                    continue
                closes = [r[3] for r in rows]
                s50 = sma(closes, SMA_TREND, i)
                if s50 is None or i < 31:
                    continue
                if any(p["sym"] == s for p in pos):
                    continue
                mom = closes[i] / closes[i - 31] - 1
                if bear:
                    if closes[i] < s50:
                        cands.append((mom, s, closes[i], -1))
                elif closes[i] > s50:
                    cands.append((mom, s, closes[i], 1))
            cands.sort(reverse=not bear)
            picks = [] if not gate_ok else cands[:TOPN]
            for _, s, px, side in picks:
                if len(pos) >= TOPN:
                    break
                rows = data[s]
                hi = [r[1] for r in rows]
                lo = [r[2] for r in rows]
                cl = [r[3] for r in rows]
                vol = atr_frac(hi, lo, cl, 14, i) or 0.03
                risk_pct = min(STOP_ATR * vol, 0.25)
                size = book * RISK / vol if vol > 0 else 0
                size = min(size, book / TOPN)
                qty = size / px
                if qty * px < 5:
                    continue
                pos.append({"sym": s, "qty": qty, "entry": px, "side": side,
                            "risk_pct": risk_pct, "i": i})
        equity.append((ts, book + sum((data[p["sym"]][i][3] - p["entry"]) * p["qty"] * p["side"]
                                      for p in pos)))
    # benchmark
    bench = None
    if btc_bench and btc:
        a = start or btc[0][0]
        z = end or btc[-1][0]
        w = [r for r in btc if a <= r[0] <= z]
        if len(w) > 1:
            bench = (w[-1][3] / w[0][3] - 1) * 100
    return trades, equity, bench


def stats(trades, equity, bench=None):
    if not trades:
        return {"n": 0}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    peak = -1e18
    dd = 0.0
    for _, e in equity:
        peak = max(peak, e)
        dd = min(dd, e / peak - 1)
    out = {"n": len(trades), "wr": round(len(wins) / len(trades) * 100, 1),
           "pnl": round(sum(pnls), 2),
           "pnl_pct": round((equity[-1][1] / BOOK - 1) * 100, 2),
           "exp_pct": round(sum(t["ret"] for t in trades) / len(trades) * 100, 3),
           "maxdd_pct": round(dd * 100, 2)}
    if bench is not None:
        out["bench_pct"] = round(bench, 2)
        out["vs_bench_pp"] = round(out["pnl_pct"] - bench, 2)
    return out


def timeframe_disagreement(conn, lo="2026-09-01", hi="2026-10-17"):
    """How often does the 15m dip opener take the OPPOSITE side to the 1d
    regime trader on the same symbol? (Measured at the DIP trigger: a 15m
    capitulation candle with no bearish regime filter fires LONG in any tape.)
    """
    rows = conn.execute(
        "select symbol, open_time_utc, close, open from candles "
        "where timeframe='15m' and symbol like '%/USDT' "
        "and open_time_utc >= strftime('%s',?) * 1000 "
        "and open_time_utc <= strftime('%s',?) * 1000",
        (lo, hi)).fetchall()
    btc = conn.execute(
        "select open_time_utc, close from candles where symbol='BTC/USDT' "
        "and timeframe='1d' order by open_time_utc").fetchall()
    bcl = [r[1] for r in btc]
    days = {}
    for i, (ts, c) in enumerate(btc):
        days[ts] = {"bear": c < (sma(bcl, 200, i) or c), "close": c}
    longs_bear = 0
    total = 0
    for sym, ts, c, o in rows:
        d = ts - ts % 86_400_000
        day = days.get(d)
        if day is None:
            continue
        if (c / o - 1) <= -0.03:     # the dip condition the 15m opener hunts
            total += 1
            if day["bear"]:
                longs_bear += 1
    return {"dip_candles": total, "in_bear_days": longs_bear,
            "pct": round(longs_bear / total * 100, 1) if total else 0.0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    ap.add_argument("--no-cost", action="store_true")
    a = ap.parse_args()
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    data = load(conn)
    end = data["BTCUSDT"][-1][0]
    day = 86_400_000
    res = {"generated_at": datetime.now(timezone.utc).isoformat(),
           "cost_rt": 0 if a.no_cost else COST_RT, "end_utc": end}
    # expanding walk-forward: 3 folds, no parameter is fitted here, so the
    # split only answers "does the same shipped rule hold in each period".
    starts = [end - 1000 * day, end - 730 * day, end - 365 * day]
    for k, st in enumerate(starts, 1):
        tr, eq, bench = run(data, 200, cost=not a.no_cost, start=st)
        res[f"fold{k}_200d"] = stats(tr, eq, bench)
    tr, eq, bench = run(data, 200, cost=not a.no_cost, start=end - 1000 * day)
    res["full_1000d"] = stats(tr, eq, bench)
    # the shipped default BEFORE champion's override — the docstring's number
    tr, eq, bench = run(data, 150, cost=not a.no_cost, start=end - 1000 * day)
    res["full_1000d_sma150_const"] = stats(tr, eq, bench)
    # regime-mix sanity: how much of each window is BEAR?
    bcl = [r[3] for r in data["BTCUSDT"]]
    mix = {}
    for k, st in enumerate(starts, 1):
        idx = [i for i, r in enumerate(data["BTCUSDT"]) if r[0] >= st]
        bears = sum(1 for i in idx if bcl[i] < (sma(bcl, 200, i) or bcl[i]))
        mix[f"fold{k}"] = {"bars": len(idx), "bear_bars": bears,
                           "bear_pct": round(bears / len(idx) * 100, 1) if idx else 0}
    res["regime_mix"] = mix
    res["timeframe_disagreement"] = timeframe_disagreement(conn)
    print(json.dumps(res, indent=2))
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=2))


if __name__ == "__main__":
    sys.exit(main())
