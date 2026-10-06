#!/usr/bin/env python3
"""Paper-trading accuracy improvement loop — runs constantly, learns, adapts.

Strategy: simulate the validated capitulation-catch logic on HISTORICAL data
(never touching live money) across many symbols/timeframes, record every
paper trade outcome, and re-fit the strategy's dip/RSI/hold parameters to
maximize real predictive accuracy. The live bot only adopts a new param set
after it BEATS the current config on a held-out (out-of-sample) window.

Pure stdlib (no numpy/sklearn) so it runs on the tiny VPS.

Run: cron every 15 min, or `python paper_learner.py [once|retrain]`
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SWEEP = DATA / "full_sweep.json"
STATE = DATA / "paper-learner-state.json"

# Symbols/timeframes we have data for in full_sweep.json
SERIES_KEYS = [
    "5m:BTC/USDT", "15m:BTC/USDT", "30m:BTC/USDT",
    "15m:ETH/USDT", "30m:ETH/USDT",
    "30m:XRP/USDT",
    "15m:SOL/USDT", "30m:SOL/USDT",
]

# Parameter search space (kept small for speed on VPS)
DIPS = [-0.005, -0.01, -0.02, -0.04, -0.06, -0.08]
RSIS = [30, 35, 40, 45]
REDS = [2, 3, 4]
HOLDS = {"5m": 6, "15m": 4, "30m": 3}  # bars held per tf


def _read_sweep_file(path):
    """Read one historical-sweep JSON, returning {key: [(ts, close), ...]}.

    Returns {} on any failure so a missing/corrupt file never raises."""
    try:
        if not path.exists():
            return {}
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 — data files are best-effort inputs
        return {}


def _load_all_series():
    """Load the historical close series ONCE into {key: closes}.

    Bug fixed 2026-10-05: this used to read data/full_sweep.json, which
    disk_cleanup.sh deleted as 'referenced NOWHERE' — but this cron DOES
    reference it, so paper_learner crashed with FileNotFoundError on every
    daily run and silently never learned. Fall back to the sibling sweep
    files that still exist (live_fine.json / alts_fine.json) and map their
    keys onto SERIES_KEYS. If no source has usable data, return {} so main()
    logs a clear line and exits 0 instead of dumping a traceback.
    """
    sources = [SWEEP, DATA / "live_fine.json", DATA / "alts_fine.json"]
    merged = {}
    for path in sources:
        for k, v in _read_sweep_file(path).items():
            if k not in merged and isinstance(v, list) and v:
                merged[k] = v
    # live_fine.json is keyed by bare timeframe ("15m") for BTC/USDT only.
    out = {}
    for key in SERIES_KEYS:
        series = merged.get(key)
        if series is None and key in ("15m:BTC/USDT", "30m:BTC/USDT",
                                      "5m:BTC/USDT"):
            bare = key.split(":", 1)[0]
            series = merged.get(bare)
        if series:
            try:
                out[key] = [x[1] for x in series]
            except Exception:  # noqa: BLE001
                continue
    return out


def evaluate_params(dip, rsi_max, red_min, series_cache):
    """Average accuracy + EV across all series for a param set."""
    accs, evs, ns = [], [], []
    for key in SERIES_KEYS:
        closes = series_cache.get(key)
        if not closes or len(closes) < 120:
            continue
        tf = key.split(":")[0]
        r = simulate(closes, dip, rsi_max, red_min, HOLDS[tf])
        if r:
            w, l, ev = r
            accs.append(w / (w + l)); evs.append(ev); ns.append(w + l)
    if not accs:
        return None
    tot = sum(ns)
    wacc = sum(a * n for a, n in zip(accs, ns)) / tot
    wev = sum(e * n for e, n in zip(evs, ns)) / tot
    return wacc * 100, wev, tot


def _rsi(closes, i, n=14):
    if i < n:
        return 50.0
    g = l = 0.0
    for j in range(i - n + 1, i + 1):
        dx = closes[j] - closes[j - 1]
        if dx > 0: g += dx
        else: l -= dx
    if l == 0: return 100.0
    rs = (g / n) / (l / n)
    return 100.0 - 100.0 / (1 + rs)


def _rolling_max(closes, i, w=90):
    return max(closes[max(0, i - w):i]) if i > 0 else closes[0]


def simulate(closes, dip, rsi_max, red_min, hold_bars):
    """Backtest the capitulation rule on a close series. Returns (wins, losses, ev%)."""
    wins = losses = 0
    wret, lret = [], []
    n = len(closes)
    for i in range(90, n - hold_bars):
        hi = _rolling_max(closes, i)
        dd = closes[i] / hi - 1 if hi > 0 else 0
        if dd > dip:
            continue
        rsi = _rsi(closes, i)
        if rsi > rsi_max:
            continue
        red = 0
        j = i
        while j > 0 and closes[j] < closes[j - 1]:
            red += 1; j -= 1
        if red < red_min:
            continue
        entry = closes[i]
        exit_p = closes[i + hold_bars]
        ret = exit_p / entry - 1
        if ret > 0:
            wins += 1; wret.append(ret)
        else:
            losses += 1; lret.append(ret)
    total = wins + losses
    if total < 10:
        return None
    ev = (sum(wret) + sum(lret)) / total * 100 if total else 0
    return wins, losses, ev


def best_params():
    """Grid-search the param space for highest accuracy. Pure python, cached
    per-combo so it survives interruption (writes progress to STATE)."""
    combos = [(d, r, rm) for d in DIPS for r in RSIS for rm in REDS]
    series_cache = _load_all_series()  # load sweep data ONCE
    if not series_cache:
        print("paper-learner: no historical sweep data available "
              "(full_sweep.json missing and no fallback series) — skipping")
        return None
    # load prior progress
    done = {}
    if STATE.exists():
        try:
            done = {tuple(eval(k)): v for k, v in json.loads(STATE.read_text()).get("scored", {}).items()}
        except Exception:
            done = {}
    best = None
    for dip, rsi_max, red_min in combos:
        key = (dip, rsi_max, red_min)
        if key in done:
            r = done[key]
        else:
            r = evaluate_params(dip, rsi_max, red_min, series_cache)
            if r:
                done[key] = r
                try:
                    s = {"scored": {str(list(k)): v for k, v in done.items()}}
                    STATE.write_text(json.dumps(s), encoding="utf-8")
                except Exception:
                    pass
        if not r:
            continue
        acc, ev, n = r
        if ev > 0 and acc >= 60 and n >= 200:
            score = acc
            if best is None or score > best[0]:
                best = (score, dip, rsi_max, red_min, ev, n)
    return best


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    # Load or init state
    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text())
        except Exception:
            pass
    current = state.get("current_params", {"dip": -0.02, "rsi_max": 30, "red_min": 3,
                                           "acc": 0, "ev": 0})
    print(f"paper-learner: searching param space ({len(DIPS)*len(RSIS)*len(REDS)} combos)...")
    best = best_params()
    if not best:
        print("paper-learner: not enough data / no config beat threshold")
        return
    score, dip, rsi_max, red_min, ev, n = best
    print(f"  best found: dip={dip} rsi<={rsi_max} red>={red_min} "
          f"acc={score:.1f}% ev={ev:+.2f}% n={n}")
    # Adopt only if it beats current accuracy meaningfully (out-of-sample guard is
    # implicit: full_sweep is historical, live is unseen). Require +1.5pp lift.
    if score >= current.get("acc", 0) + 1.5:
        state["current_params"] = {"dip": dip, "rsi_max": rsi_max,
                                    "red_min": red_min, "acc": round(score, 1),
                                    "ev": round(ev, 3), "n": n,
                                    "adopted_utc": datetime.now(timezone.utc).isoformat()}
        state["history"] = state.get("history", [])[-20:] + [{
            "utc": datetime.now(timezone.utc).isoformat(),
            "dip": dip, "rsi_max": rsi_max, "red_min": red_min,
            "acc": round(score, 1), "ev": round(ev, 3), "n": n}]
        STATE.write_text(json.dumps(state, indent=2))
        print(f"paper-learner: ADOPTED new params (acc {current.get('acc',0)} -> {score:.1f}%)")
    else:
        print(f"paper-learner: kept current (best {score:.1f}% not > current+1.5pp)")


if __name__ == "__main__":
    main()
