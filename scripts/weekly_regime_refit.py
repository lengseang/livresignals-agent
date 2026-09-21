#!/usr/bin/env python3
"""Weekly regime-edge re-fit (Layer 4 — the learner).

Walk-forward re-measures the capitulation-catch edge PER REGIME from recent
30m BTC data and writes the result to a JSON file the adaptive engine reads
for sizing. This is how the system "adapts over time": if a regime's edge
decays, the engine automatically sizes it down (or stops trading it).

Output: data/regime-edges.json
  { "<REGIME>": {"win_rate": 0.xx, "ev": 0.00xx, "n": int, "as_of_utc": "..."} }

Consumes the edge table in adaptive_engine.py via REGIME_EDGE/REGIME_WR,
which falls back to static defaults if this file is absent.
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

FINE = Path(__file__).resolve().parents[1] / "data" / "live_fine.json"  # persistent
EDGES_PATH = Path(__file__).resolve().parents[1] / "data" / "regime-edges.json"

DIP = -0.02
VM = 0.004
RM = 3
RMAX = 30
HOLD = 12


def load_closes():
    if Path(FINE).exists():
        return [c[1] for c in json.load(open(FINE))["30m"]]
    # fallback: DB 1h candles (coarser but available)
    import sqlite3
    from trading.config import SQLITE_PATH
    con = sqlite3.connect(str(SQLITE_PATH))
    rows = con.execute(
        "SELECT close FROM candles WHERE symbol='BTC/USDT' AND timeframe='1h' "
        "ORDER BY open_time_utc ASC").fetchall()
    con.close()
    return [float(r[0]) for r in rows]


def precompute(c):
    n = len(c)
    dd = [0.0] * n
    vol = [0.0] * n
    rsi = [50.0] * n
    red = [0] * n
    slope = [0.0] * n
    for i in range(90, n):
        hi = max(c[i - 90:i])
        dd[i] = c[i] / hi - 1.0 if hi > 0 else 0.0
    for i in range(14, n):
        r = [c[j] / c[j - 1] - 1 for j in range(i - 13, i)]
        vol[i] = statistics.pstdev(r) if len(r) > 1 else 0.0
    for i in range(14, n):
        g = l = 0.0
        for j in range(i - 13, i + 1):
            d = c[j] - c[j - 1]
            if d >= 0:
                g += d
            else:
                l -= d
        if l == 0:
            rsi[i] = 100.0
        else:
            rs = (g / 14) / (l / 14)
            rsi[i] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(1, n):
        red[i] = red[i - 1] + 1 if c[i] < c[i - 1] else 0
    for i in range(90, n):
        seg = c[i - 90:i]
        m = len(seg)
        x = list(range(m))
        xm = (m - 1) / 2
        ym = sum(seg) / m
        denom = sum((xi - xm) ** 2 for xi in x) or 1
        num = sum((x[j] - xm) * (seg[j] - ym) for j in range(m))
        slope[i] = (num / denom) / (ym or 1)
    return dd, vol, rsi, red, slope


def refit(c):
    dd, vol, rsi, red, slope = precompute(c)
    # use most recent 40% as the "recent regime" window (adapt to current market)
    start = int(len(c) * 0.6)
    buckets = {}
    for i in range(max(90, start), len(c) - HOLD):
        if dd[i] > DIP or vol[i] < VM or red[i] < RM or rsi[i] > RMAX:
            continue
        trend = "TREND" if slope[i] > 0.0005 else ("DOWNTREND" if slope[i] < -0.0005 else "RANGE")
        voltag = "HIVOL" if vol[i] > 0.004 else "LOVOL"
        key = f"{trend}+{voltag}"
        fwd = c[i + HOLD] / c[i] - 1
        b = buckets.setdefault(key, {"wins": 0, "losses": 0, "rets": []})
        if fwd > 0:
            b["wins"] += 1
        else:
            b["losses"] += 1
        b["rets"].append(fwd)

    out = {}
    for k, b in buckets.items():
        total = b["wins"] + b["losses"]
        if total < 15:  # too few to trust — omit (engine uses static default)
            continue
        wr = b["wins"] / total
        ev = sum(b["rets"]) / total
        out[k] = {"win_rate": round(wr, 3), "ev": round(ev, 4), "n": total}
    out["as_of_utc"] = datetime.now(timezone.utc).isoformat()
    return out


def main():
    c = load_closes()
    edges = refit(c)
    EDGES_PATH.parent.mkdir(parents=True, exist_ok=True)
    EDGES_PATH.write_text(json.dumps(edges, indent=2), encoding="utf-8")
    print(json.dumps(edges, indent=2))


if __name__ == "__main__":
    main()
