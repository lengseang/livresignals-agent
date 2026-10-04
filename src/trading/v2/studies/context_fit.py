"""Fit + walk-forward evaluation of the gateway's CONTEXT model (gateway-entry-v2), on exactly the features the live
pipeline computes (`trading.v2.daily_context`), with the label the live bracket trades: a long hits the approved +7%
before -3.5% within 14 days (1h bars, both in one bar = loss). Research only.

Walk-forward: for each OOS year Y in 2023..2026, L2 chosen on Y-1 (fit < Y-1), refit on < Y, purged by the label
horizon. Pooled OOS: AUC, Brier skill vs the expanding base rate. Trading: enter when P >= break-even + 0.05, the shipped
bracket + lock-half ratchet, admission bar of V2-16 (mean > 0, Bonferroni-6 week-block LB > 0, >= 3 of 4 years > 0,
Brier skill > 0). The runtime model is then fitted on ALL purged data, L2 chosen on the last full year; its `shrink`
is the pooled OOS Gini, so the gateway can only move from the base rate as far as the evidence earned.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from trading.v2.daily_context import CONTEXT_FIELDS, CONTEXT_VERSION, H, N_CTX_1H, UNIVERSE, coin_row, cross_section
from trading.v2.studies.entry_study import (
    END, OOS_YEARS, ROUND_TRIP, STEP, admitted, load_1h, simulate, summarize,
)
from trading.v2.studies.gateway_study import auc, fit_logistic, logloss, predict
from trading.v2.studies.history import utc_ms

STOP, TARGET = 0.035, 0.07
LABEL_BARS = 14 * 24
L2_GRID = (0.1, 1.0, 10.0, 100.0)
BE = (STOP + ROUND_TRIP) / (TARGET + STOP)
MARGIN = 0.05
# evidence only: NOT loaded by the gateway (its pooled OOS Brier skill is negative on the label the bracket trades)
MODEL_PATH = Path(__file__).resolve().parents[4] / "docs" / "v2" / "evidence" / "gateway_context_model.json"


def points(data: dict) -> dict:
    t0 = min(int(d["t"][0]) for d in data.values())
    grid = np.arange(t0 - t0 % STEP + STEP, END, STEP)
    out = {s: [] for s in data}
    for T in grid:
        rows, idx = {}, {}
        for s, d in data.items():
            i = int(np.searchsorted(d["t"], T - H, side="right")) - 1
            if i < N_CTX_1H or i + 1 >= len(d["t"]) or d["t"][i] != T - H or d["t"][i + 1] != T:
                continue
            r = coin_row(d["t"], d["o"], d["h"], d["l"], d["c"], d["qv"], i)
            if r is not None:
                rows[s], idx[s] = r, i
        if "BTCUSDT" not in rows or len(rows) < 3:
            continue
        b = data["BTCUSDT"]; ib = idx["BTCUSDT"]
        above = 1.0 if b["c"][ib] > b["c"][ib - 1199:ib + 1].mean() else 0.0
        for s, r in rows.items():
            full = {**r, **cross_section(rows, s, rows["BTCUSDT"], above)}
            out[s].append((int(T), idx[s] + 1, [float(full[f]) for f in CONTEXT_FIELDS]))
    res = {}
    for s, rr in out.items():
        if rr:
            res[s] = {"T": np.array([x[0] for x in rr], dtype=np.int64), "entry": np.array([x[1] for x in rr]),
                      "x": np.array([x[2] for x in rr], dtype=float)}
            y = np.full(len(rr), np.nan)
            for k, e in enumerate(res[s]["entry"]):
                if int(e) + LABEL_BARS < len(data[s]["c"]):
                    r = simulate(data[s], int(e), STOP, ratchet=False, horizon_bars=LABEL_BARS)
                    if r is not None:
                        y[k] = r[3]
            res[s]["y"] = y
    return res


def stack(pts: dict, lo: int, hi: int, purge: bool = True):
    xs, ys = [], []
    cut = LABEL_BARS * H + STEP if purge else 0
    for p in pts.values():
        m = (p["T"] >= lo) & (p["T"] < hi - cut) & np.isfinite(p["y"]) & np.all(np.isfinite(p["x"]), 1)
        xs.append(p["x"][m]); ys.append(p["y"][m])
    return np.vstack(xs), np.concatenate(ys)


def fit(pts: dict, val_year: int, upto: int):
    xf, yf = stack(pts, 0, utc_ms(val_year, 1))
    xv, yv = stack(pts, utc_ms(val_year, 1), utc_ms(val_year + 1, 1))
    mu, sd = xf.mean(0), xf.std(0) + 1e-12
    val = {l2: logloss(predict(fit_logistic((xf - mu) / sd, yf, l2), (xv - mu) / sd), yv) for l2 in L2_GRID}
    l2 = min(val, key=val.get)
    xt, yt = stack(pts, 0, upto)
    mu, sd = xt.mean(0), xt.std(0) + 1e-12
    return {"w": fit_logistic((xt - mu) / sd, yt, l2), "mean": mu, "std": sd, "l2": l2, "base": float(yt.mean()),
            "n": int(len(yt)), "val": {str(k): v for k, v in val.items()}}


def prob(m: dict, x: np.ndarray) -> np.ndarray:
    ok = np.all(np.isfinite(x), 1)
    return np.where(ok, predict(m["w"], (np.where(ok[:, None], x, 0) - m["mean"]) / m["std"]), np.nan)


def trades(data, pts, m, lo, hi):
    out = []
    for s, p in pts.items():
        pr = prob(m, p["x"]); free = lo
        for k in range(len(p["T"])):
            T = int(p["T"][k])
            if T < max(free, lo) or T >= hi or not (pr[k] >= BE + MARGIN):
                continue
            r = simulate(data[s], int(p["entry"][k]), STOP, ratchet=True, end_ms=hi, horizon_bars=LABEL_BARS)
            if r is None:
                continue
            net, xi, why, _ = r
            from trading.v2.studies.entry_study import _year
            out.append({"sym": s, "t": T, "net": net, "why": why, "year": _year(T)})
            ex = int(data[s]["t"][xi]) + H
            free = ex - ex % STEP + STEP
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    data = {s: d for s in UNIVERSE if (d := load_1h(a.data, s)) is not None}
    pts = points(data)
    wf, tr, pb, pb0, pp_all, yy_all = {}, [], 0.0, 0.0, [], []
    for y in OOS_YEARS:
        m = fit(pts, y - 1, utc_ms(y, 1))
        lo, hi = utc_ms(y, 1), min(utc_ms(y + 1, 1), END)
        x, yy = stack(pts, lo, hi, purge=False)
        pp = predict(m["w"], (x - m["mean"]) / m["std"])
        b, b0 = float(((pp - yy) ** 2).sum()), float(((m["base"] - yy) ** 2).sum())
        pb += b; pb0 += b0; pp_all.append(pp); yy_all.append(yy)
        wf[y] = {"l2": m["l2"], "n_train": m["n"], "train_base": m["base"], "oos_n": int(len(yy)),
                 "oos_base": float(yy.mean()), "oos_auc": auc(pp, yy), "oos_brier_skill": 1 - b / b0}
        tr += trades(data, pts, m, lo, hi)
    pooled_auc = auc(np.concatenate(pp_all), np.concatenate(yy_all))
    skill = 1 - pb / pb0
    s = summarize(tr)
    s["admitted"] = admitted(s) and skill > 0
    final = fit(pts, 2025, END)
    shrink = max(0.0, 2 * pooled_auc - 1) if skill > 0 else 0.0     # skill must show in BOTH ranking and calibration
    res = {"context_version": CONTEXT_VERSION, "label": "long +7% before -3.5% within 14d (1h bars, both = loss)",
           "walk_forward": {str(k): v for k, v in wf.items()}, "pooled_oos_auc": pooled_auc,
           "pooled_oos_brier_skill": skill, "trading": s, "break_even": BE,
           "final": {"l2": final["l2"], "n": final["n"], "base": final["base"], "shrink": shrink}}
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "context_fit.json").write_text(json.dumps(res, indent=1, default=float))
    MODEL_PATH.write_text(json.dumps({
        "model_version": "gateway-entry-v2", "context_version": CONTEXT_VERSION, "columns": list(CONTEXT_FIELDS),
        "label": res["label"], "trained_on": f"{len(data)} coins 1h, 2020-01..2026-08 (purged)",
        "break_even_p": BE, "admitted_for_entries": bool(s["admitted"]), "evidence": "docs/v2/evidence/context_fit.json",
        "model": {"w": final["w"].tolist(), "mean": final["mean"].tolist(), "std": final["std"].tolist(),
                  "train_base_rate": final["base"], "shrink": shrink}}, indent=1))
    for y, v in wf.items():
        print(y, f"auc {v['oos_auc']:.3f} brier_skill {v['oos_brier_skill']:+.4f} base {v['oos_base']:.3f} n {v['oos_n']}")
    print(f"pooled auc {pooled_auc:.3f} brier skill {skill:+.4f} shrink {shrink:.3f}")
    print(f"trading n={s.get('trades')} mean={s.get('mean', 0)*100:+.3f}% lbB={s.get('lb_bonferroni', 0)*100:+.3f}% "
          f"years={ {k: round(v*100, 2) for k, v in s.get('per_year_mean', {}).items()} } admitted={s['admitted']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
