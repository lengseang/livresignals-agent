"""Pre-registered gateway entry-model study (docs/loop/designs/V2-15 section 4).

Label: at each hourly decision point, does a long hit +TARGET before -STOP within 14 days (a bar touching both
counts as a loss; a timeout counts as not hit). Inputs: the bar-derived features-v1 numbers (history/FEATURE_COLUMNS).
Model: L2 logistic regression (Newton/IRLS, numpy only), standardised on the fit set; the L2 strength is chosen by
fitting on 2023 and validating on 2024 (time-ordered), then refit on all of TRAIN. TEST is evaluated once.

The fitted coefficients are written to `src/trading/v2/gateway_model.json`, which the gateway loads at runtime:
the numbers used live are exactly the numbers evaluated here.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from trading.v2.studies.exit_study import (
    DECISION_EVERY_MS, FEE, GRID, MAX_HOLD_MS, SIDE_COST, STOP, SYMBOLS, TARGET, TEST, TRAIN,
    bootstrap_lb, metrics, run_symbol,
)
from trading.v2.studies.history import FEATURE_COLUMNS, is_finite_rows, load, utc_ms

L2_GRID = (0.01, 0.1, 1.0, 10.0, 100.0)
MARGINS = (0.0, 0.05, 0.10)
ROUND_TRIP_COST = 2 * (FEE + SIDE_COST)
BREAK_EVEN_P = (STOP + ROUND_TRIP_COST) / (TARGET + STOP)     # p*T - (1-p)*S - costs = 0
MODEL_PATH = Path(__file__).resolve().parents[1] / "gateway_model.json"


def labels(d: dict, side: str = "LONG") -> np.ndarray:
    """1 if the barrier on the profit side is hit first, 0 if the stop is (or both in one bar, or timeout); NaN if
    there are no bars after the point."""
    mo, o, h, l = d["m_open"], d["m_o"], d["m_h"], d["m_l"]
    y = np.full(len(d["f_ts"]), np.nan)
    for j, t in enumerate(d["f_ts"]):
        i0 = int(np.searchsorted(mo, t, side="left"))
        if i0 >= len(mo) or mo[i0] - t > 5 * 60_000:
            continue
        i1 = int(np.searchsorted(mo, mo[i0] + MAX_HOLD_MS, side="left"))
        ef = o[i0] * (1 + SIDE_COST) if side == "LONG" else o[i0] * (1 - SIDE_COST)
        if side == "LONG":
            win, loss = h[i0:i1] >= ef * (1 + TARGET), l[i0:i1] <= ef * (1 - STOP)
        else:
            win, loss = l[i0:i1] <= ef * (1 - TARGET), h[i0:i1] >= ef * (1 + STOP)
        iw = int(np.argmax(win)) if win.any() else None
        il = int(np.argmax(loss)) if loss.any() else None
        y[j] = 1.0 if iw is not None and (il is None or iw < il) else 0.0
    return y


def fit_logistic(x: np.ndarray, y: np.ndarray, l2: float, iters: int = 50) -> np.ndarray:
    """IRLS for L2-penalised logistic regression; returns weights incl. intercept (index 0, not penalised)."""
    X = np.hstack([np.ones((len(x), 1)), x])
    w = np.zeros(X.shape[1])
    pen = np.full(X.shape[1], l2); pen[0] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(X @ w, -30, 30)))
        g = X.T @ (p - y) + pen * w
        H = (X * (p * (1 - p))[:, None]).T @ X + np.diag(pen) + 1e-9 * np.eye(len(w))
        step = np.linalg.solve(H, g)
        w -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return w


def predict(w: np.ndarray, x: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-np.clip(w[0] + x @ w[1:], -30, 30)))


def logloss(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def auc(p, y) -> float:
    order = np.argsort(p)
    r = np.empty(len(p)); r[order] = np.arange(1, len(p) + 1)
    n1 = y.sum(); n0 = len(y) - n1
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)) if n1 and n0 else float("nan")


def calib_table(p, y, bins: int = 10) -> list[dict]:
    qs = np.quantile(p, np.linspace(0, 1, bins + 1))
    out = []
    for a, b in zip(qs[:-1], qs[1:]):
        m = (p >= a) & (p <= b)
        if m.sum():
            out.append({"p_lo": float(a), "p_hi": float(b), "n": int(m.sum()),
                        "mean_pred": float(p[m].mean()), "hit_rate": float(y[m].mean())})
    return out


def table(ds: dict, side: str = "LONG"):
    rows = {}
    for s, d in ds.items():
        x, y, t = d["f_x"], labels(d, side), d["f_ts"]
        ok = is_finite_rows(x) & np.isfinite(y)
        rows[s] = (t[ok], x[ok], y[ok])
    return rows


def split(rows, lo, hi):
    xs, ys = [], []
    for t, x, y in rows.values():
        m = (t >= lo) & (t < hi)
        xs.append(x[m]); ys.append(y[m])
    return np.vstack(xs), np.concatenate(ys)


def gated_trades(ds: dict, model: dict, thr: float, period, rule) -> list[dict]:
    allr = []
    for s, d in ds.items():
        x = d["f_x"]
        ok = is_finite_rows(x)
        z = (np.where(ok[:, None], x, 0.0) - model["mean"]) / model["std"]
        p = predict(np.array(model["w"]), z)
        gate = ok & (p >= thr)
        allr += run_symbol(d, rule, *period, gate=gate)
    return allr


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--cache", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    ds = {s: load(a.data, a.cache, s) for s in SYMBOLS}
    res: dict = {"break_even_p": BREAK_EVEN_P, "columns": list(FEATURE_COLUMNS)}
    models = {}
    for side in ("LONG", "SHORT"):
        rows = table(ds, side)
        y2024 = utc_ms(2024, 1)
        x_fit, y_fit = split(rows, TRAIN[0], y2024)
        x_val, y_val = split(rows, y2024, TRAIN[1])
        mu, sd = x_fit.mean(0), x_fit.std(0) + 1e-12
        val = {}
        for l2 in L2_GRID:
            w = fit_logistic((x_fit - mu) / sd, y_fit, l2)
            val[l2] = logloss(predict(w, (x_val - mu) / sd), y_val)
        best_l2 = min(val, key=val.get)
        x_tr, y_tr = split(rows, *TRAIN)
        mu, sd = x_tr.mean(0), x_tr.std(0) + 1e-12
        w = fit_logistic((x_tr - mu) / sd, y_tr, best_l2)
        base = float(y_tr.mean())
        x_te, y_te = split(rows, *TEST)
        p_te = predict(w, (x_te - mu) / sd)
        brier = float(((p_te - y_te) ** 2).mean()); brier0 = float(((base - y_te) ** 2).mean())
        # out-of-sample skill decides how far the gateway may move from the base rate: Gini = 2 AUC - 1, floored at 0
        models[side] = {"w": w.tolist(), "mean": mu.tolist(), "std": sd.tolist(), "l2": best_l2,
                        "train_base_rate": base, "n_train": int(len(y_tr)),
                        "shrink": max(0.0, 2 * auc(p_te, y_te) - 1)}
        res[side] = {"l2_validation_logloss": {str(k): v for k, v in val.items()}, "l2": best_l2,
                     "train_base_rate": base, "test_base_rate": float(y_te.mean()), "n_train": int(len(y_tr)),
                     "n_test": int(len(y_te)), "test_brier": brier, "test_brier_base": brier0,
                     "test_brier_skill": 1 - brier / brier0, "test_auc": auc(p_te, y_te),
                     "test_calibration": calib_table(p_te, y_te),
                     "coef_std_units": dict(zip(FEATURE_COLUMNS, w[1:].tolist()))}
        print(f"{side}: l2={best_l2} base train {base:.3f} test {y_te.mean():.3f}  Brier skill {1 - brier / brier0:+.4f}  "
              f"AUC {auc(p_te, y_te):.3f}")

    # trading admission test (LONG only: spot). Margin chosen on 2024 with a model fitted on 2023 ONLY.
    rule_pre = next(r for r in GRID if r.name == "R0_stop_only")        # the pre-registered frozen exit choice
    rule_ship = next(r for r in GRID if r.name == "R3_lock_0.50")       # the shipped L1 rule (bracket + lock half)
    rows = table(ds, "LONG")
    x_fit, y_fit = split(rows, TRAIN[0], utc_ms(2024, 1))
    mu, sd = x_fit.mean(0), x_fit.std(0) + 1e-12
    m2023 = {"w": fit_logistic((x_fit - mu) / sd, y_fit, res["LONG"]["l2"]).tolist(), "mean": mu, "std": sd}
    margin_val = {}
    for mg in MARGINS:
        tr = gated_trades(ds, m2023, BREAK_EVEN_P + mg, (utc_ms(2024, 1), TRAIN[1]), rule_ship)
        margin_val[mg] = metrics(tr)
    pick = max((m for m in MARGINS if margin_val[m]["trades"] >= 30), key=lambda m: margin_val[m]["mean"], default=0.0)
    thr = BREAK_EVEN_P + pick
    final = {"w": models["LONG"]["w"], "mean": np.array(models["LONG"]["mean"]), "std": np.array(models["LONG"]["std"])}
    adm = {}
    for name, rule in (("pre_registered_R0", rule_pre), ("shipped_R3_lock_0.50", rule_ship)):
        g = gated_trades(ds, final, thr, TEST, rule)
        base_tr = []
        for s, d in ds.items():
            base_tr += run_symbol(d, rule, *TEST)
        r = np.array([x["net"] for x in g])
        adm[name] = {"gated": metrics(g), "gated_mean_lb95": bootstrap_lb(r) if len(r) else None,
                     "ungated_entry_set_a": metrics(base_tr)}
    passed = all(v["gated"].get("trades", 0) > 0 and v["gated"]["mean"] > 0 and (v["gated_mean_lb95"] or -1) > 0
                 for v in adm.values()) and res["LONG"]["test_brier_skill"] > 0
    res["admission"] = {"margin_validation_2024": {str(k): v for k, v in margin_val.items()}, "margin": pick,
                        "threshold": thr, "test": adm, "passed": passed,
                        "bar": "TEST mean net per trade > 0 with 95% bootstrap LB > 0 (both exit rules) AND Brier skill > 0"}
    print("admission threshold", round(thr, 4), "passed:", passed)
    for k, v in adm.items():
        gm = v["gated"]
        print(f"  {k}: gated n={gm.get('trades')} mean={gm.get('mean', float('nan'))*100:+.3f}% "
              f"lb95={(v['gated_mean_lb95'] or float('nan'))*100:+.3f}%  ungated mean={v['ungated_entry_set_a']['mean']*100:+.3f}%")
    # TRAIN-period feature quantiles: the gateway's heuristic answers are scaled by these, not by invented constants
    x_tr, _ = split(table(ds, "LONG"), *TRAIN)
    col = {c: x_tr[:, i] for i, c in enumerate(FEATURE_COLUMNS)}
    qs = (0.1, 0.25, 0.5, 0.75, 0.9, 0.99)
    quant = {name: {str(q): float(np.quantile(v, q)) for q in qs} for name, v in (
        ("atr", col["atr"]), ("volume_zscore", col["volume_zscore"]), ("abs_ret_15m", np.abs(col["ret_15m"])),
        ("abs_ret_1h", np.abs(col["ret_1h"])), ("abs_distance_vwap", np.abs(col["distance_vwap"])),
        ("abs_ema_fast_slope", np.abs(col["ema_fast_slope"])))}
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "gateway_study.json").write_text(json.dumps(res, indent=1))
    MODEL_PATH.write_text(json.dumps({
        "model_version": "gateway-entry-v1", "feature_version": "features-v1", "columns": list(FEATURE_COLUMNS),
        "label": f"hit +{TARGET:.0%} before -{STOP:.0%} within 14d (both in one bar = loss)",
        "trained_on": "BTCUSDT ETHUSDT SOLUSDT 1m, 2023-01..2024-12", "break_even_p": BREAK_EVEN_P,
        "admitted_for_entries": passed, "evidence": "docs/v2/evidence/gateway_study.json",
        "train_quantiles": quant,
        "models": models}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
