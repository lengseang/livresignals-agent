"""Pre-registered exit study (docs/loop/designs/V2-15): which in-position rule keeps a near-target trade from
riding back to its stop, measured on real history with costs, train/test split frozen in the design note.

Simulation (per symbol, long only, one position at a time):
  * a decision point every 4h (UTC) while flat; entry at the OPEN of the first 1m bar at/after it;
  * the protective stop applies from the entry bar on; a ratchet stop computed at the close of bar t-1 applies
    to bar t (never the bar it was computed on: no look-ahead); the ATR used is the last CLOSED hour's;
  * stop and target touched in the same bar -> the stop (conservative); a gap through the stop fills at the open
    (worse), a gap through the target fills at the open (better);
  * timeout after MAX_HOLD_MS at that bar's close.
Costs: fee per side, half spread + slippage per side (the approved paper model).
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import numpy as np

from trading.v2.studies.history import FEATURE_COLUMNS, load, utc_ms, wilder_atr_frac

STOP, TARGET = 0.035, 0.07
FEE, HALF_SPREAD_BPS, SLIP_BPS = 0.001, 0.41, 1.0
SIDE_COST = (HALF_SPREAD_BPS + SLIP_BPS) / 1e4
MAX_HOLD_MS = 14 * 86_400_000
DECISION_EVERY_MS = 4 * 3_600_000
TRAIN = (utc_ms(2023, 1), utc_ms(2025, 1))
TEST = (utc_ms(2025, 1), utc_ms(2026, 10))
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
GIVEBACK_MFE = 0.5          # "reached at least half the target, then closed at a loss"


@dataclass(frozen=True)
class Rule:
    name: str
    target: bool = True
    be_at: Optional[float] = None          # R2: stop -> breakeven (+costs) once MFE >= be_at * TARGET
    lock_at: Optional[float] = None        # R3: once MFE >= lock_at * TARGET, stop >= entry + lock_frac * MFE
    lock_frac: float = 0.5
    trail_at: Optional[float] = None       # R4/R5: once MFE >= trail_at * TARGET, stop >= high - k * ATR
    trail_k: Optional[float] = None
    target_mult: float = 1.0               # take profit at target_mult * TARGET


GRID = (
    Rule("R0_stop_only", target=False),
    Rule("R1_bracket"),
    Rule("R2_be_0.50", be_at=0.5), Rule("R2_be_0.75", be_at=0.75),
    Rule("R3_lock_0.50", lock_at=0.5), Rule("R3_lock_0.75", lock_at=0.75),
    Rule("R4_trail_0.25_k2", trail_at=0.25, trail_k=2), Rule("R4_trail_0.25_k3", trail_at=0.25, trail_k=3),
    Rule("R4_trail_0.50_k2", trail_at=0.5, trail_k=2), Rule("R4_trail_0.50_k3", trail_at=0.5, trail_k=3),
    Rule("R5_chandelier_k2", target=False, trail_at=0.0, trail_k=2),
    Rule("R5_chandelier_k3", target=False, trail_at=0.0, trail_k=3),
)


def stop_path(rule: Rule, entry_fill: float, h: np.ndarray, atr_px: np.ndarray) -> np.ndarray:
    """Stop level in force on each bar of the trade (index 0 = entry bar). Monotone non-decreasing."""
    n = len(h)
    init = entry_fill * (1 - STOP)
    s = np.full(n, init)
    if n == 1:
        return s
    run_hi = np.maximum.accumulate(h)[:-1]          # best high known at the CLOSE of the previous bar
    mfe = run_hi / entry_fill - 1.0
    cand = np.full(n - 1, init)
    if rule.be_at is not None:
        be = entry_fill * (1 + 2 * (FEE + SIDE_COST))
        cand = np.where(mfe >= rule.be_at * TARGET, np.maximum(cand, be), cand)
    if rule.lock_at is not None:
        lock = entry_fill * (1 + rule.lock_frac * mfe)
        cand = np.where(mfe >= rule.lock_at * TARGET, np.maximum(cand, lock), cand)
    if rule.trail_k is not None:
        trail = run_hi - rule.trail_k * atr_px[:-1]
        on = (mfe >= rule.trail_at * TARGET) & np.isfinite(trail)
        cand = np.where(on, np.maximum(cand, trail), cand)
    s[1:] = np.maximum.accumulate(cand)              # a stop never loosens
    return s


def simulate_trade(rule: Rule, o, h, l, c, atr_px):
    """One trade over the bars from the entry bar to the hold limit. Returns (net_return, mfe, reason, n_bars)."""
    entry_mid = o[0]
    entry_fill = entry_mid * (1 + SIDE_COST)
    stops = stop_path(rule, entry_fill, h, atr_px)
    hit_stop = l <= stops
    i_stop = int(np.argmax(hit_stop)) if hit_stop.any() else None
    i_tgt = None
    tgt = entry_fill * (1 + TARGET * rule.target_mult)
    if rule.target:
        hit_t = h >= tgt
        i_tgt = int(np.argmax(hit_t)) if hit_t.any() else None
    if i_stop is not None and (i_tgt is None or i_stop <= i_tgt):
        i, px, why = i_stop, min(stops[i_stop], o[i_stop]), "stop"
    elif i_tgt is not None:
        i, px, why = i_tgt, max(tgt, o[i_tgt]), "target"
    else:
        i, px, why = len(c) - 1, c[-1], "timeout"
    exit_fill = px * (1 - SIDE_COST)
    net = exit_fill * (1 - FEE) / (entry_fill * (1 + FEE)) - 1.0
    mfe = float(h[: i + 1].max() / entry_fill - 1.0)
    return net, mfe, why, i


def run_symbol(d: dict, rule: Rule, start: int, end: int, gate: Optional[np.ndarray] = None) -> list[dict]:
    """Sequential trades on one symbol in [start, end). `gate`: optional bool per feature row (entry filter)."""
    mo, o, h, l, c = d["m_open"], d["m_o"], d["m_h"], d["m_l"], d["m_c"]
    hr_end = d["h_end"]
    atr_h = wilder_atr_frac(d["h_h"], d["h_l"], d["h_c"])
    # ATR known at each minute = the last hourly bar that ENDED at or before the minute's close
    idx_h = np.searchsorted(hr_end, mo + 60_000, side="right") - 1
    atr_frac_m = np.where(idx_h >= 0, atr_h[np.clip(idx_h, 0, None)], np.nan)
    allowed = None
    if gate is not None:
        allowed = {int(t) for t, g in zip(d["f_ts"], gate) if g}
    trades = []
    t = start - start % DECISION_EVERY_MS + (DECISION_EVERY_MS if start % DECISION_EVERY_MS else 0)
    while t < end:
        if allowed is not None and t not in allowed:
            t += DECISION_EVERY_MS
            continue
        i0 = int(np.searchsorted(mo, t, side="left"))
        if i0 >= len(mo) or mo[i0] - t > 5 * 60_000:      # no bar near the decision (exchange halt / data hole)
            t += DECISION_EVERY_MS
            continue
        # a trade never reads bars past the period end (a TRAIN trade must not see TEST prices): it times out there
        i1 = int(min(np.searchsorted(mo, mo[i0] + MAX_HOLD_MS, side="left"), np.searchsorted(mo, end, side="left")))
        if i1 - i0 < 2:
            break
        sl = slice(i0, i1)
        net, mfe, why, k = simulate_trade(rule, o[sl], h[sl], l[sl], c[sl], atr_frac_m[sl] * c[sl])
        exit_ms = int(mo[i0 + k] + 60_000)
        trades.append({"t": int(mo[i0]), "net": net, "mfe": mfe, "why": why, "mins": k + 1})
        t = exit_ms - exit_ms % DECISION_EVERY_MS + DECISION_EVERY_MS
    return trades


def metrics(tr: list[dict]) -> dict:
    if not tr:
        return {"trades": 0}
    r = np.array([x["net"] for x in tr])
    win, loss = r[r > 0].sum(), -r[r < 0].sum()
    give = sum(1 for x in tr if x["mfe"] >= GIVEBACK_MFE * TARGET and x["net"] < 0)
    reached = sum(1 for x in tr if x["mfe"] >= GIVEBACK_MFE * TARGET)
    return {"trades": len(tr), "win_rate": float((r > 0).mean()), "mean": float(r.mean()), "sum": float(r.sum()),
            "profit_factor": float(win / loss) if loss > 0 else None,
            "giveback_rate": give / len(tr), "giveback_of_reached": (give / reached) if reached else None,
            "reasons": {k: sum(1 for x in tr if x["why"] == k) for k in ("stop", "target", "timeout")},
            "median_hold_h": float(np.median([x["mins"] for x in tr]) / 60)}


def bootstrap_lb(r: np.ndarray, n: int = 2000, seed: int = 7) -> float:
    rng = np.random.default_rng(seed)
    means = rng.choice(r, size=(n, len(r)), replace=True).mean(axis=1)
    return float(np.quantile(means, 0.025))


def entry_sets(d: dict) -> dict:
    """(a) every decision point; (b) features-v1 regime trend bullish at the decision point."""
    bull = d["f_x"][:, FEATURE_COLUMNS.index("trend_bullish")] > 0.5
    return {"a_all": None, "b_bullish": bull}


def study(datasets: dict, period: tuple[int, int]) -> dict:
    out = {}
    for rule in GRID:
        per = {}
        for es in ("a_all", "b_bullish"):
            allr = []
            for sym, d in datasets.items():
                tr = run_symbol(d, rule, *period, gate=entry_sets(d)[es])
                per[f"{es}/{sym}"] = metrics(tr)
                allr += tr
            per[f"{es}/ALL"] = metrics(allr)
            if allr:
                per[f"{es}/ALL"]["mean_lb95"] = bootstrap_lb(np.array([x["net"] for x in allr]))
        cells = [per[f"{es}/{s}"]["mean"] for es in ("a_all", "b_bullish") for s in datasets if per[f"{es}/{s}"]["trades"]]
        gives = [per[f"{es}/ALL"]["giveback_rate"] for es in ("a_all", "b_bullish")]
        out[rule.name] = {"rule": asdict(rule), "selection_mean": float(np.mean(cells)),
                          "selection_giveback": float(np.mean(gives)), "cells": per}
    return out


def select(train: dict) -> str:
    return sorted(train, key=lambda k: (-round(train[k]["selection_mean"], 6), train[k]["selection_giveback"]))[0]


# ---------------------------------------------------------------- POST-HOC paired analysis (not pre-registered)
# The owner's question, isolated from entry quality and turnover: SAME entries, and only trades that reached
# half the target. All these rules are identical until MFE first reaches 0.5 x target, so the paired difference
# is exactly the effect of what is done from that moment on. Block bootstrap by UTC week (overlapping trades).
PAIRED = (
    Rule("hold_bracket"),                                     # R1: keep stop -3.5% / target +7%
    Rule("take_at_half", target_mult=0.5),                    # close at +3.5%
    Rule("breakeven", be_at=0.5),
    Rule("lock_half", lock_at=0.5),
    Rule("trail_k2", trail_at=0.5, trail_k=2), Rule("trail_k3", trail_at=0.5, trail_k=3),
    Rule("trail_k3_no_target", target=False, trail_at=0.5, trail_k=3),
)


def paired(datasets: dict, period: tuple[int, int], every_ms: int = DECISION_EVERY_MS) -> dict:
    rows = []      # (week, {rule: net})
    for sym, d in datasets.items():
        mo, o, h, l, c = d["m_open"], d["m_o"], d["m_h"], d["m_l"], d["m_c"]
        atr_h = wilder_atr_frac(d["h_h"], d["h_l"], d["h_c"])
        idx_h = np.searchsorted(d["h_end"], mo + 60_000, side="right") - 1
        atr_m = np.where(idx_h >= 0, atr_h[np.clip(idx_h, 0, None)], np.nan)
        end_i = int(np.searchsorted(mo, period[1], side="left"))
        t = period[0]
        while t < period[1]:
            i0 = int(np.searchsorted(mo, t, side="left"))
            t += every_ms
            if i0 >= end_i or mo[i0] - (t - every_ms) > 5 * 60_000:
                continue
            i1 = int(min(np.searchsorted(mo, mo[i0] + MAX_HOLD_MS, side="left"), end_i))
            if i1 - i0 < 2:
                continue
            sl = slice(i0, i1)
            ef = o[i0] * (1 + SIDE_COST)
            reach = h[sl] >= ef * (1 + 0.5 * TARGET)
            stop_first = l[sl] <= ef * (1 - STOP)
            if not reach.any():
                continue
            k = int(np.argmax(reach))
            if stop_first.any() and int(np.argmax(stop_first)) <= k:
                continue                                 # stopped before it ever got halfway
            res = {r.name: simulate_trade(r, o[sl], h[sl], l[sl], c[sl], atr_m[sl] * c[sl])[0] for r in PAIRED}
            rows.append((int(mo[i0] // (7 * 86_400_000)), res))
    out = {"n": len(rows), "rules": {}}
    if not rows:
        return out
    weeks = np.array([w for w, _ in rows])
    uniq = np.unique(weeks)
    rng = np.random.default_rng(11)
    base = np.array([r["hold_bracket"] for _, r in rows])
    for rule in PAIRED:
        x = np.array([r[rule.name] for _, r in rows])
        diff = x - base
        by_w = {w: diff[weeks == w] for w in uniq}
        boots = []
        for _ in range(2000):
            pick = rng.choice(uniq, size=len(uniq), replace=True)
            boots.append(np.concatenate([by_w[w] for w in pick]).mean())
        out["rules"][rule.name] = {"mean": float(x.mean()), "win_rate": float((x > 0).mean()),
                                   "loss_rate": float((x < 0).mean()),
                                   "diff_vs_hold": float(diff.mean()),
                                   "diff_ci95": [float(np.quantile(boots, 0.025)), float(np.quantile(boots, 0.975))]}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", required=True, type=Path, help="root with klines_1m/<SYM>/<SYM>-1m-YYYY-MM.zip")
    ap.add_argument("--cache", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--phase", choices=("train", "test", "paired"), required=True,
                    help="train: rank the grid on TRAIN and freeze the choice; test: evaluate once, needs the frozen choice")
    a = ap.parse_args(argv)
    ds = {s: load(a.data, a.cache, s) for s in SYMBOLS}
    a.out.mkdir(parents=True, exist_ok=True)
    frozen = a.out / "exit_study_train.json"
    if a.phase == "paired":
        res = {"label": "POST-HOC (not pre-registered): trades that reached half the target, same entries",
               "train": paired(ds, TRAIN), "test": paired(ds, TEST)}
        (a.out / "exit_study_paired.json").write_text(json.dumps(res, indent=1))
        for per in ("train", "test"):
            print(per, "n =", res[per]["n"])
            for k, v in res[per]["rules"].items():
                print(f"  {k:20s} mean {v['mean']*100:+.3f}%  win {v['win_rate']:.2f}  loss {v['loss_rate']:.2f}  "
                      f"vs hold {v['diff_vs_hold']*100:+.3f}% CI [{v['diff_ci95'][0]*100:+.3f}, {v['diff_ci95'][1]*100:+.3f}]")
        return 0
    if a.phase == "train":
        tr = study(ds, TRAIN)
        choice = select(tr)
        frozen.write_text(json.dumps({"period": TRAIN, "choice": choice, "results": tr}, indent=1, default=float))
        print("TRAIN choice:", choice)
        for k, v in sorted(tr.items(), key=lambda kv: -kv[1]["selection_mean"]):
            print(f"  {k:24s} mean {v['selection_mean']*100:+.3f}%  giveback {v['selection_giveback']:.3f}")
        return 0
    if not frozen.exists():
        raise SystemExit("run --phase train first: the test is evaluated against a FROZEN choice")
    choice = json.loads(frozen.read_text())["choice"]
    te = study(ds, TEST)
    (a.out / "exit_study_test.json").write_text(json.dumps({"period": TEST, "frozen_choice": choice, "results": te},
                                                           indent=1, default=float))
    print("TEST (frozen choice:", choice, ")")
    for k, v in sorted(te.items(), key=lambda kv: -kv[1]["selection_mean"]):
        print(f"  {k:24s} mean {v['selection_mean']*100:+.3f}%  giveback {v['selection_giveback']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
