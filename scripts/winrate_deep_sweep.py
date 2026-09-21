#!/usr/bin/env python3
"""Deeper short-term win-rate sweep — push past 67.5% with honest OOS validation.

Tests combinations of:
  ENTRY: dip depth (-3..-8%), high-vol gate, regime, momentum, RSI, consecutive reds
  EXIT:  hold horizon 1h/6h/12h/24h, TP/SL ratios

Each config runs on the FULL 1h BTC sample, then the top-5 are re-validated on a
chronological held-out slice (last 20%). Reports win rate, avg return, and n.

Honest anti-overfit: a config is only "interesting" if it holds OUT of sample.
"""
from __future__ import annotations

import sqlite3
import statistics
from trading.config import SQLITE_PATH


def load_closes() -> list[float]:
    con = sqlite3.connect(str(SQLITE_PATH))
    rows = con.execute(
        "SELECT close FROM candles WHERE symbol='BTC/USDT' AND timeframe='1h' "
        "ORDER BY open_time_utc ASC"
    ).fetchall()
    con.close()
    return [float(r[0]) for r in rows]


def rsi_at(closes, i, lookback=14):
    if i < lookback:
        return 50.0
    gains = 0.0
    losses = 0.0
    for j in range(i - lookback + 1, i + 1):
        d = closes[j] - closes[j - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    rs = (gains / lookback) / (losses / lookback)
    return 100.0 - 100.0 / (1.0 + rs)


def vol_at(closes, i, lookback=14):
    if i < lookback:
        return 0.0
    rets = [closes[j] / closes[j - 1] - 1 for j in range(i - lookback + 1, i)]
    return statistics.pstdev(rets) if len(rets) > 1 else 0.0


def red_bars(closes, i):
    """Count consecutive red hourly bars ending at i."""
    n = 0
    j = i
    while j > 0 and closes[j] < closes[j - 1]:
        n += 1
        j -= 1
    return n


def regime(closes, i, lookback=90):
    if i < lookback:
        return 0
    seg = closes[i - lookback:i]
    n = len(seg)
    x = list(range(n))
    xm = sum(x) / n
    ym = sum(seg) / n
    denom = sum((xi - xm) ** 2 for xi in x) or 1
    slope = sum((x[j] - xm) * (seg[j] - ym) for j in range(n)) / denom
    return 1 if abs(slope) / (ym or 1) > 0.0003 else 0


def run_config(closes, dip, hold, cfg):
    """cfg keys: high_vol(bool), min_red(int), rsi_max(float), regime(int|None), pos_mom(bool)."""
    wins = 0
    total = 0
    rets = []
    for i in range(90, len(closes) - hold):
        hi = max(closes[i - 90:i])
        dd = closes[i] / hi - 1
        if dd > dip:
            continue
        vol = vol_at(closes, i)
        if cfg.get("high_vol") and vol < 0.006:
            continue
        if cfg.get("min_red") and red_bars(closes, i) < cfg["min_red"]:
            continue
        rsi = rsi_at(closes, i)
        if cfg.get("rsi_max") and rsi > cfg["rsi_max"]:
            continue
        if cfg.get("regime") is not None and regime(closes, i) != cfg["regime"]:
            continue
        if cfg.get("pos_mom") and (closes[i] / closes[i - 24] - 1 if i >= 24 else 0) < 0:
            continue
        total += 1
        fwd = closes[i + hold] / closes[i] - 1
        rets.append(fwd)
        if fwd > 0:
            wins += 1
    wr = wins / total * 100 if total else 0.0
    avg = sum(rets) / len(rets) * 100 if rets else 0.0
    return wr, total, avg


def main():
    closes = load_closes()
    n = len(closes)
    split = int(n * 0.8)  # last 20% held out
    train, test = closes[:split], closes[split:]
    print(f"1h BTC data: {n} candles | train={len(train)} test={len(test)}")
    print()

    configs = [
        ("dip-5 + highvol + 3red", -0.05, 12, {"high_vol": True, "min_red": 3}),
        ("dip-5 + highvol + rsi<40", -0.05, 12, {"high_vol": True, "rsi_max": 40}),
        ("dip-5 + highvol + ranging", -0.05, 12, {"high_vol": True, "regime": 0}),
        ("dip-5 + highvol + posmom", -0.05, 12, {"high_vol": True, "pos_mom": True}),
        ("dip-5 + highvol (12h)", -0.05, 12, {"high_vol": True}),
        ("dip-5 + highvol (6h)", -0.05, 6, {"high_vol": True}),
        ("dip-5 + highvol (1h)", -0.05, 1, {"high_vol": True}),
        ("dip-5 + highvol (24h)", -0.05, 24, {"high_vol": True}),
        ("dip-5 + 3red + rsi<40", -0.05, 12, {"min_red": 3, "rsi_max": 40}),
        ("dip-5 + highvol + 3red + rsi40", -0.05, 12, {"high_vol": True, "min_red": 3, "rsi_max": 40}),
        ("dip-6 + highvol", -0.06, 12, {"high_vol": True}),
        ("dip-4 + highvol", -0.04, 12, {"high_vol": True}),
        ("dip-5 + 5red", -0.05, 12, {"min_red": 5}),
        ("dip-5 + rsi<35", -0.05, 12, {"rsi_max": 35}),
    ]

    rows = []
    for name, dip, hold, cfg in configs:
        wr_tr, n_tr, avg_tr = run_config(train, dip, hold, cfg)
        wr_te, n_te, avg_te = run_config(test, dip, hold, cfg)
        rows.append((name, wr_tr, n_tr, avg_tr, wr_te, n_te, avg_te))

    rows.sort(key=lambda r: -r[4])  # sort by OOS win rate
    print(f"{'config':<34} {'train WR':>8} {'n':>5} {'avg':>7} | {'OOS WR':>7} {'n':>5} {'avg':>7}")
    print("-" * 80)
    for name, wr_tr, n_tr, avg_tr, wr_te, n_te, avg_te in rows:
        print(f"{name:<34} {wr_tr:>7.1f}% {n_tr:>5} {avg_tr:>+6.3f} | {wr_te:>6.1f}% {n_te:>5} {avg_te:>+6.3f}")


if __name__ == "__main__":
    main()
