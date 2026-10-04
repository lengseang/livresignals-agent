"""Pre-registered entry-edge study v2 (docs/loop/designs/V2-16 section 3). Research only: no order path, no network.

15 coins, Binance spot 1h, 2020-01..2026-08. Long only, one position per coin, a decision every 4h (UTC) while flat.
Volatility-scaled barrier: stop = clamp(1.5 x ATR14(daily)/close, 1%, 5%), target = 2 x stop, horizon 7 days, managed with
the shipped bracket + lock-half ratchet, simulated on 1h bars (stop first when a bar touches both: conservative).
Walk-forward OOS years 2023, 2024, 2025, 2026(..08); 2020-2022 are only ever training / warm-up.

Every feature at decision time t uses bars that ENDED at or before t (the last closed hour, the last closed UTC day).
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from trading.replay.loader import iter_symbol
from trading.v2.studies.exit_study import FEE, SIDE_COST
from trading.v2.studies.gateway_study import auc, fit_logistic, logloss, predict
from trading.v2.studies.history import utc_ms

UNIVERSE = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT", "LINKUSDT", "DOGEUSDT", "AVAXUSDT",
            "NEARUSDT", "SUIUSDT", "ARBUSDT", "DOTUSDT", "LTCUSDT", "ATOMUSDT")
H = 3_600_000
DAY = 24 * H
STEP = 4 * H
HORIZON_BARS = 7 * 24
WARMUP_BARS = 30 * 24 + 24
OOS_YEARS = (2023, 2024, 2025, 2026)
END = utc_ms(2026, 9)
ROUND_TRIP = 2 * (FEE + SIDE_COST)
L2_GRID = (0.1, 1.0, 10.0, 100.0)
MARGIN = 0.05
ALPHA = 0.05 / 6                      # Bonferroni over the six candidates
FEATURES = ("z1d", "z3d", "z7d", "z14d", "z30d", "ema20_gap", "ema50_gap", "ema20_slope", "donchian_pos", "rsi_d",
            "atr_d", "vol_ratio", "xs_rank7", "xs_rank30", "rel_btc7", "btc_ret30", "btc_above_ema50", "qv_ratio")


# ----------------------------------------------------------------------------- data

def load_1h(root: Path, sym: str) -> Optional[dict]:
    rows = [(b.open_ms, b.open, b.high, b.low, b.close, b.quote_volume) for b in iter_symbol(root, sym, interval="1h")
            if b.open_ms < END]
    if len(rows) < WARMUP_BARS + HORIZON_BARS:
        return None
    a = np.array(rows, dtype=float)
    return {"t": a[:, 0].astype(np.int64), "o": a[:, 1], "h": a[:, 2], "l": a[:, 3], "c": a[:, 4], "qv": a[:, 5]}


def ema(x: np.ndarray, span: int) -> np.ndarray:
    a = 2.0 / (span + 1.0)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def daily(d: dict) -> dict:
    """UTC daily bars from 1h bars; a day is used only once all its hours are closed (end = day start + 24h)."""
    day = d["t"] // DAY
    starts = np.flatnonzero(np.r_[True, day[1:] != day[:-1]])
    ends = np.r_[starts[1:], len(day)]
    o = d["o"][starts]; c = d["c"][ends - 1]
    h = np.array([d["h"][s:e].max() for s, e in zip(starts, ends)])
    l = np.array([d["l"][s:e].min() for s, e in zip(starts, ends)])
    end_ms = (day[starts] + 1) * DAY
    return {"end": end_ms, "o": o, "h": h, "l": l, "c": c}


def wilder(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) <= n:
        return out
    a = x[1:n + 1].mean()
    out[n] = a
    for i in range(n + 1, len(x)):
        a = (a * (n - 1) + x[i]) / n
        out[i] = a
    return out


def daily_indicators(dd: dict) -> dict:
    c, h, l = dd["c"], dd["h"], dd["l"]
    tr = np.r_[h[0] - l[0], np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))]
    atr = wilder(tr, 14) / c
    ch = np.r_[0.0, np.diff(c)]
    up, dn = wilder(np.maximum(ch, 0), 14), wilder(np.maximum(-ch, 0), 14)
    rsi = np.where(dn > 0, 100 - 100 / (1 + up / np.where(dn > 0, dn, 1)), 100.0)
    return {"end": dd["end"], "atr": atr, "rsi": rsi}


# ----------------------------------------------------------------------------- features at decision points

def coin_features(d: dict) -> dict:
    c, t = d["c"], d["t"]
    lr = np.r_[0.0, np.diff(np.log(c))]
    def roll_std(x, w):
        s = np.convolve(x, np.ones(w), "full")[:len(x)]
        s2 = np.convolve(x * x, np.ones(w), "full")[:len(x)]
        var = np.maximum(s2 / w - (s / w) ** 2, 0)
        return np.sqrt(var)
    dvol = roll_std(lr, 720) * math.sqrt(24)
    vol7 = roll_std(lr, 168); vol30 = roll_std(lr, 720)
    e20, e50 = ema(c, 20 * 24), ema(c, 50 * 24)
    qv7 = np.convolve(d["qv"], np.ones(168), "full")[:len(c)]
    qv30 = np.convolve(d["qv"], np.ones(720), "full")[:len(c)]
    hh = np.array([d["h"][max(0, i - 480):i].max() if i > 0 else np.nan for i in range(len(c))])
    ll = np.array([d["l"][max(0, i - 480):i].min() if i > 0 else np.nan for i in range(len(c))])
    return {"lr": lr, "dvol": dvol, "vol7": vol7, "vol30": vol30, "e20": e20, "e50": e50, "qv7": qv7, "qv30": qv30,
            "hh": hh, "ll": ll}


def ret(c: np.ndarray, i: int, k: int) -> float:
    return c[i] / c[i - k] - 1.0 if i - k >= 0 else np.nan


def build_points(data: dict) -> dict:
    """For every coin and every 4h decision time: the features, the barrier, the entry index."""
    feats = {s: coin_features(d) for s, d in data.items()}
    dind = {s: daily_indicators(daily(d)) for s, d in data.items()}
    btc = data["BTCUSDT"]; bf = feats["BTCUSDT"]
    t0 = min(int(d["t"][0]) for d in data.values())
    grid = np.arange(t0 - t0 % STEP + STEP, END, STEP)
    pts = {s: [] for s in data}
    for T in grid:
        # i = last closed 1h bar (end = open + H <= T); entry at bar i + 1 which opens at T
        row_ret = {}
        for s, d in data.items():
            i = int(np.searchsorted(d["t"], T - H, side="right")) - 1
            if i < WARMUP_BARS or i + 1 >= len(d["t"]) or d["t"][i + 1] != T or d["t"][i] != T - H:
                continue
            row_ret[s] = (i, ret(d["c"], i, 168), ret(d["c"], i, 720))
        if "BTCUSDT" not in row_ret or len(row_ret) < 3:
            continue
        r7 = {s: v[1] for s, v in row_ret.items()}; r30 = {s: v[2] for s, v in row_ret.items()}
        def rank(m, s):
            vals = sorted(m.values())
            return (vals.index(m[s])) / (len(vals) - 1)
        ib = row_ret["BTCUSDT"][0]
        btc_ret30 = row_ret["BTCUSDT"][2]
        btc_above = 1.0 if btc["c"][ib] > bf["e50"][ib] else 0.0
        for s, (i, a7, a30) in row_ret.items():
            d, f, di = data[s], feats[s], dind[s]
            j = int(np.searchsorted(di["end"], T, side="right")) - 1      # last CLOSED day
            if j < 20 or not math.isfinite(di["atr"][j]) or not math.isfinite(f["dvol"][i]) or f["dvol"][i] <= 0:
                continue
            c = d["c"]
            dv = f["dvol"][i]
            z = [ret(c, i, 24 * k) / (dv * math.sqrt(k)) for k in (1, 3, 7, 14, 30)]
            span = f["hh"][i] - f["ll"][i]
            x = z + [c[i] / f["e20"][i] - 1, c[i] / f["e50"][i] - 1, f["e20"][i] / f["e20"][i - 24] - 1,
                     (c[i] - f["ll"][i]) / span if span > 0 else 0.5, di["rsi"][j], di["atr"][j],
                     f["vol7"][i] / f["vol30"][i] if f["vol30"][i] > 0 else 1.0,
                     rank(r7, s), rank(r30, s), a7 - r7["BTCUSDT"], btc_ret30, btc_above,
                     (f["qv7"][i] / 168) / (f["qv30"][i] / 720) if f["qv30"][i] > 0 else 1.0]
            stop = min(0.05, max(0.01, 1.5 * di["atr"][j]))
            breakout = 1.0 if c[i] > f["hh"][i] else 0.0
            pts[s].append((int(T), i + 1, stop, breakout, x))
    out = {}
    for s, rows in pts.items():
        if rows:
            out[s] = {"T": np.array([r[0] for r in rows], dtype=np.int64), "entry": np.array([r[1] for r in rows]),
                      "stop": np.array([r[2] for r in rows]), "breakout": np.array([r[3] for r in rows]),
                      "x": np.array([r[4] for r in rows], dtype=float)}
    return out


# ----------------------------------------------------------------------------- outcome simulation (1h bars)

def simulate(d: dict, e: int, stop_pct: float, ratchet: bool = True, end_ms: int = END):
    """Trade entered at the open of bar e. Returns (net, exit_bar_index, reason, label_hit_target_first)."""
    o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
    e1 = min(e + HORIZON_BARS, len(c), int(np.searchsorted(t, end_ms, side="left")))
    if e1 - e < 2:
        return None
    ef = o[e] * (1 + SIDE_COST)
    tgt = ef * (1 + 2 * stop_pct)
    stops = np.full(e1 - e, ef * (1 - stop_pct))
    if ratchet:
        run_hi = np.maximum.accumulate(h[e:e1])[:-1]
        mfe = run_hi / ef - 1
        lock = np.where(mfe >= 0.5 * 2 * stop_pct, ef * (1 + 0.5 * mfe), -np.inf)
        stops[1:] = np.maximum(stops[1:], np.maximum.accumulate(lock))
    hs = l[e:e1] <= stops
    ht = h[e:e1] >= tgt
    i_s = int(np.argmax(hs)) if hs.any() else None
    i_t = int(np.argmax(ht)) if ht.any() else None
    if i_s is not None and (i_t is None or i_s <= i_t):
        k, px, why = i_s, min(stops[i_s], o[e + i_s]), "stop"
    elif i_t is not None:
        k, px, why = i_t, max(tgt, o[e + i_t]), "target"
    else:
        k, px, why = e1 - e - 1, c[e1 - 1], "timeout"
    net = px * (1 - SIDE_COST) * (1 - FEE) / (ef * (1 + FEE)) - 1
    # label (plain bracket): target strictly before the initial stop
    init_s = l[e:e1] <= ef * (1 - stop_pct)
    j_s = int(np.argmax(init_s)) if init_s.any() else None
    label = 1.0 if (i_t is not None and (j_s is None or i_t < j_s)) else 0.0
    return net, e + k, why, label


def labels(data: dict, pts: dict) -> None:
    for s, p in pts.items():
        y = np.full(len(p["T"]), np.nan)
        for k, (e, st) in enumerate(zip(p["entry"], p["stop"])):
            r = simulate(data[s], int(e), float(st), ratchet=False)
            if r is not None and int(e) + HORIZON_BARS < len(data[s]["c"]):
                y[k] = r[3]
        p["y"] = y


# ----------------------------------------------------------------------------- candidates

def rule_gate(name: str, p: dict) -> np.ndarray:
    x = p["x"]; col = {f: x[:, i] for i, f in enumerate(FEATURES)}
    z7, z30 = col["z7d"], col["z30d"]
    if name == "F1_tsmom":
        return (z30 > 0) & (z7 > 0)
    if name == "F2_xs_top_third":
        return col["xs_rank30"] >= 2 / 3
    if name == "F3_breakout20d":
        return p["breakout"] > 0.5
    if name == "F4_tsmom_btc_regime":
        return (z30 > 0) & (z7 > 0) & (col["btc_above_ema50"] > 0.5)
    if name == "F5_rsi_reversion":
        return col["rsi_d"] < 30
    if name == "ALL_points":
        return np.ones(len(x), dtype=bool)
    raise ValueError(name)


def break_even(stop: np.ndarray) -> np.ndarray:
    return (stop + ROUND_TRIP) / (3 * stop)


def model_gates(pts: dict, year: int) -> tuple[dict, dict]:
    """Walk-forward M for OOS `year`: L2 chosen on year-1 (fit < year-1, purged), refit on < year (purged)."""
    y0, y1 = utc_ms(year - 1, 1), utc_ms(year, 1)
    purge = HORIZON_BARS * H + STEP

    def stack(lo, hi):
        xs, ys = [], []
        for p in pts.values():
            m = (p["T"] >= lo) & (p["T"] < hi - purge) & np.isfinite(p["y"]) & np.all(np.isfinite(p["x"]), 1)
            xs.append(p["x"][m]); ys.append(p["y"][m])
        return np.vstack(xs), np.concatenate(ys)
    xf, yf = stack(0, y0)
    xv, yv = [], []
    for p in pts.values():
        m = (p["T"] >= y0) & (p["T"] < y1 - purge) & np.isfinite(p["y"]) & np.all(np.isfinite(p["x"]), 1)
        xv.append(p["x"][m]); yv.append(p["y"][m])
    xv, yv = np.vstack(xv), np.concatenate(yv)
    mu, sd = xf.mean(0), xf.std(0) + 1e-12
    val = {l2: logloss(predict(fit_logistic((xf - mu) / sd, yf, l2), (xv - mu) / sd), yv) for l2 in L2_GRID}
    l2 = min(val, key=val.get)
    xt, yt = stack(0, y1)
    mu, sd = xt.mean(0), xt.std(0) + 1e-12
    w = fit_logistic((xt - mu) / sd, yt, l2)
    base = float(yt.mean())
    gates, probs = {}, {}
    for s, p in pts.items():
        ok = np.all(np.isfinite(p["x"]), 1)
        pr = np.where(ok, predict(w, (np.where(ok[:, None], p["x"], 0) - mu) / sd), 0.0)
        probs[s] = pr
        gates[s] = ok & (pr >= break_even(p["stop"]) + MARGIN)
    return gates, {"l2": l2, "val_logloss": {str(k): v for k, v in val.items()}, "train_base_rate": base,
                   "coef": dict(zip(FEATURES, w[1:].tolist())), "probs": probs}


def trade_rule(data: dict, pts: dict, gates: dict, lo: int, hi: int) -> list[dict]:
    """Sequential trades per coin in [lo, hi): enter at a gated decision point when flat."""
    out = []
    for s, p in pts.items():
        d = data[s]
        free_from = lo
        for k in range(len(p["T"])):
            T = int(p["T"][k])
            if T < free_from or T < lo or T >= hi or not gates[s][k]:
                continue
            r = simulate(d, int(p["entry"][k]), float(p["stop"][k]), ratchet=True, end_ms=hi)
            if r is None:
                continue
            net, xi, why, _ = r
            exit_ms = int(d["t"][xi]) + H
            out.append({"sym": s, "t": T, "net": net, "why": why, "year": _year(T)})
            free_from = exit_ms - exit_ms % STEP + STEP
    return out


def _year(t: int) -> int:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t / 1000, tz=timezone.utc).year


def block_lb(trades: list[dict], q: float, n: int = 4000, seed: int = 17) -> float:
    if not trades:
        return float("nan")
    wk = np.array([x["t"] // (7 * DAY) for x in trades]); r = np.array([x["net"] for x in trades])
    uniq = np.unique(wk); by = {w: r[wk == w] for w in uniq}
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(n):
        pick = rng.choice(uniq, size=len(uniq), replace=True)
        means.append(np.concatenate([by[w] for w in pick]).mean())
    return float(np.quantile(means, q))


def summarize(tr: list[dict]) -> dict:
    if not tr:
        return {"trades": 0}
    r = np.array([x["net"] for x in tr])
    win, loss = r[r > 0].sum(), -r[r < 0].sum()
    years = {y: float(np.mean([x["net"] for x in tr if x["year"] == y])) for y in sorted({x["year"] for x in tr})}
    return {"trades": len(tr), "win_rate": float((r > 0).mean()), "mean": float(r.mean()), "sum": float(r.sum()),
            "profit_factor": float(win / loss) if loss > 0 else None, "per_year_mean": years,
            "lb_bonferroni": block_lb(tr, ALPHA), "lb95": block_lb(tr, 0.025),
            "reasons": {k: sum(1 for x in tr if x["why"] == k) for k in ("stop", "target", "timeout")}}


def admitted(s: dict) -> bool:
    if not s.get("trades"):
        return False
    pos_years = sum(1 for y in OOS_YEARS if s["per_year_mean"].get(y, -1) > 0)
    return s["mean"] > 0 and s["lb_bonferroni"] > 0 and pos_years >= 3


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", required=True, type=Path, help="root with klines_1h/<SYM>/<SYM>-1h-YYYY-MM.zip")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    data = {s: d for s in UNIVERSE if (d := load_1h(a.data, s)) is not None}
    pts = build_points(data)
    labels(data, pts)
    lo, hi = utc_ms(OOS_YEARS[0], 1), END
    res: dict = {"universe": sorted(data), "oos": [lo, hi], "features": list(FEATURES),
                 "n_points": {s: int(len(p["T"])) for s, p in pts.items()}, "candidates": {}}
    for name in ("ALL_points", "F1_tsmom", "F2_xs_top_third", "F3_breakout20d", "F4_tsmom_btc_regime", "F5_rsi_reversion"):
        g = {s: rule_gate(name, p) for s, p in pts.items()}
        res["candidates"][name] = summarize(trade_rule(data, pts, g, lo, hi))
    # M: walk-forward, one model per OOS year
    m_trades, m_info, brier, brier0, aucs = [], {}, [], [], []
    for y in OOS_YEARS:
        g, info = model_gates(pts, y)
        ylo, yhi = utc_ms(y, 1), min(utc_ms(y + 1, 1), END)
        m_trades += trade_rule(data, pts, g, ylo, yhi)
        pp, yy = [], []
        for s, p in pts.items():
            m = (p["T"] >= ylo) & (p["T"] < yhi) & np.isfinite(p["y"]) & np.all(np.isfinite(p["x"]), 1)
            pp.append(info["probs"][s][m]); yy.append(p["y"][m])
        pp, yy = np.concatenate(pp), np.concatenate(yy)
        brier.append(float(((pp - yy) ** 2).mean())); brier0.append(float(((info["train_base_rate"] - yy) ** 2).mean()))
        aucs.append(auc(pp, yy))
        info.pop("probs")
        m_info[y] = {**info, "oos_brier": brier[-1], "oos_brier_base": brier0[-1], "oos_auc": aucs[-1],
                     "oos_base_rate": float(yy.mean())}
    ms = summarize(m_trades)
    ms["brier_skill_pooled"] = 1 - sum(brier) / sum(brier0)
    ms["walk_forward"] = {str(k): v for k, v in m_info.items()}
    res["candidates"]["M_logistic_wf"] = ms
    for name, s in res["candidates"].items():
        ok = admitted(s) and (name != "M_logistic_wf" or s["brier_skill_pooled"] > 0) and name != "ALL_points"
        s["admitted"] = ok
    res["buy_and_hold_oos"] = {}
    for s, d in data.items():
        i0 = int(np.searchsorted(d["t"], lo)); i1 = len(d["c"]) - 1
        if i0 < i1:
            res["buy_and_hold_oos"][s] = float(d["c"][i1] / d["o"][i0] - 1)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "entry_study_v2.json").write_text(json.dumps(res, indent=1, default=float))
    for name, s in res["candidates"].items():
        if not s.get("trades"):
            print(f"{name:22s} no trades"); continue
        yrs = " ".join(f"{y}:{v*100:+.2f}" for y, v in s["per_year_mean"].items())
        print(f"{name:22s} n={s['trades']:5d} win={s['win_rate']:.2f} mean={s['mean']*100:+.3f}% "
              f"lbB={s['lb_bonferroni']*100:+.3f}% pf={s['profit_factor'] or 0:.2f} [{yrs}] admitted={s['admitted']}")
    print("M brier skill pooled", round(ms["brier_skill_pooled"], 4), "auc by year", [round(x, 3) for x in aucs])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
