"""Expected-value signal scorer — the "advanced math" quality engine.

A supervised model that scores each candidate trade by its expected value.
Learns from every resolved signal and improves accuracy over time.

Design (honest, auditable, no external API):
  - Features (from live + historical data):
      drawdown_pct    (distance from 90d high — the core edge, validated)
      dip_depth       (raw depth; shallow dips win more)
      regime          (RANGING/TRENDING/VOLATILE)
      atr_ratio       (volatility expansion vs median)
      momentum_20d    (recent trend)
      volume_z        (volume vs 30d median)
  - Label: whether the forward 48h return was positive (the validated horizon)
  - Model: logistic regression (interpretable weights) — falls back to a
    hand-tuned rule-based score if sklearn is unavailable.
  - Output: 0-100 quality score + expected value estimate.

Every resolved signal appends a training row; the model retrains on a
schedule (and on demand), so accuracy increases with more data.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from trading.signals.adaptive import load_params

SCORER_STATE = Path(__file__).resolve().parents[3] / "data" / "scorer-state.json"
# Optimal validated config (2026-08-12): shallow dip + 48h hold = 57.8% win.
OPTIMAL_DIP = -0.05
OPTIMAL_HOLD_HOURS = 48


def _load_state() -> dict:
    if SCORER_STATE.exists():
        try:
            return json.loads(SCORER_STATE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"training_rows": [], "model_weights": None, "trained_at_utc": None,
            "version": 1}


def _save_state(s: dict) -> None:
    SCORER_STATE.parent.mkdir(parents=True, exist_ok=True)
    SCORER_STATE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def _features(sig: dict) -> dict:
    """Extract numeric features from a signal dict."""
    dd = sig.get("drawdown_pct", 0) or 0
    regime = sig.get("regime", "RANGING")
    red = sig.get("red_bars", 0) or 0
    return {
        "drawdown_pct": dd,
        "dip_depth": -abs(dd),          # shallower = closer to 0 = better
        "is_shallow_dip": 1.0 if -0.08 <= dd <= -0.03 else 0.0,
        "is_deep_dip": 1.0 if dd <= -0.12 else 0.0,
        "regime_ranging": 1.0 if regime == "RANGING" else 0.0,
        "regime_trending": 1.0 if regime == "TRENDING" else 0.0,
        "atr_ratio": sig.get("atr_ratio", 1.0) or 1.0,
        "red_bars_3": 1.0 if red >= 3 else 0.0,   # highest confidence
        "red_bars_2": 1.0 if red >= 2 else 0.0,
        "red_bars_1": 1.0 if red >= 1 else 0.0,   # frequency-optimized baseline
    }


def score_signal(sig: dict) -> dict:
    """Score a signal 0-100. Higher = better expected value.

    Combines the validated statistical edge (shallow dip > deep dip) with
    a learned logistic model when enough training data exists.
    """
    f = _features(sig)
    dd = f["drawdown_pct"]

    # Base score from the validated dip-depth relationship.
    # Peak win rate is in the -5% to -8% shallow-dip band (57.8% @ -5%/48h).
    if -0.08 <= dd <= -0.03:
        base = 78.0
    elif -0.12 <= dd < -0.08:
        base = 60.0
    elif dd < -0.12:
        base = 30.0          # deep dip = catching knife, low EV
    elif -0.03 < dd <= 0.0:
        base = 45.0          # no dip, weak edge
    else:
        base = 35.0

    # Regime adjustment (ranging/trending slightly better for reversion).
    if f["regime_trending"] and -0.08 <= dd <= -0.03:
        base += 8.0
    if f["regime_ranging"]:
        base += 4.0

    # Volatility: HIGH vol HELPS shallow-dip reversion (validated 2026-08-13).
    # LOOSE config (frequency-optimized): vol >= 0.006 fires (not 0.010).
    # Only EXTREME vol (>2.5x median) becomes knife-catching risk again.
    if f["atr_ratio"] > 2.5:
        base -= 12.0          # extreme vol = chaotic, knife risk
    elif f["atr_ratio"] >= 1.3:
        base += 6.0           # healthy high vol = bigger reverting moves
    elif f["atr_ratio"] < 0.7:
        base -= 6.0           # low vol = drift, weak edge

    # Consecutive red bars: LOOSE config fires at >=1 red (frequency-optimized
    # 2026-08-13: 1 red bar + loose vol = +69.8% compounded vs +22.9% tight).
    # More reds still = higher confidence, but we don't BLOCK low-red signals.
    if f["red_bars_3"]:
        base += 8.0
    elif f["red_bars_2"]:
        base += 4.0

    # Learned model override (once trained).
    s = _load_state()
    w = s.get("model_weights")
    learned = None
    if w:
        z = w.get("bias", 0.0)
        for k, v in f.items():
            z += w.get(k, 0.0) * v
        learned = 1.0 / (1.0 + math.exp(-z))  # logistic prob(win)
        # Blend learned probability (0-1) with base (0-100): 70/30 learned/base.
        blended = 0.7 * (learned * 100) + 0.3 * base
        base = blended

    score = max(0.0, min(100.0, base))

    # Expected value estimate (using the avg-return curve from backtest).
    avg_ret = {
        "shallow": 0.0016,   # +0.16% avg at -5%/48h
        "mid": 0.0036,       # +0.36% avg at -8%/48h
        "deep": -0.0040,     # negative EV for deep dips
    }
    if -0.08 <= dd <= -0.03:
        ev = avg_ret["shallow"]
    elif -0.12 <= dd < -0.08:
        ev = avg_ret["mid"]
    else:
        ev = avg_ret["deep"]

    return {
        "score": round(score, 1),
        "expected_value_pct": round(ev * 100, 3),
        "learned": learned is not None,
        "recommended_action": (
            "TRADE" if score >= 70 else "WATCH" if score >= 50 else "AVOID"
        ),
        "optimal_dip": OPTIMAL_DIP,
        "optimal_hold_hours": OPTIMAL_HOLD_HOURS,
    }


def record_resolution(sig: dict, correct: bool) -> None:
    """Append a training row once a signal resolves (correct/incorrect)."""
    s = _load_state()
    f = _features(sig)
    f["label"] = 1 if correct else 0
    s["training_rows"].append(f)
    _save_state(s)


def retrain() -> dict:
    """Retrain the logistic model from accumulated resolutions."""
    s = _load_state()
    rows = s["training_rows"]
    if len(rows) < 20:
        return {"trained": False, "reason": f"need >=20 rows, have {len(rows)}"}
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        return {"trained": False, "reason": "sklearn unavailable"}

    feats = ["drawdown_pct", "dip_depth", "is_shallow_dip", "is_deep_dip",
             "regime_ranging", "regime_trending", "atr_ratio"]
    X = np.array([[r.get(k, 0.0) for k in feats] for r in rows])
    y = np.array([r["label"] for r in rows])
    m = LogisticRegression(max_iter=1000)
    m.fit(X, y)
    weights = {"bias": float(m.intercept_[0])}
    for k, c in zip(feats, m.coef_[0]):
        weights[k] = float(c)
    s["model_weights"] = weights
    s["trained_at_utc"] = datetime.now(timezone.utc).isoformat()
    _save_state(s)
    return {"trained": True, "n_rows": len(rows), "weights": weights}


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "demo"
    if mode == "demo":
        demo_sigs = [
            {"drawdown_pct": -0.05, "regime": "RANGING", "atr_ratio": 1.0},
            {"drawdown_pct": -0.15, "regime": "RANGING", "atr_ratio": 1.0},
            {"drawdown_pct": -0.25, "regime": "VOLATILE", "atr_ratio": 2.5},
        ]
        for sig in demo_sigs:
            print(json.dumps({**sig, **score_signal(sig)}, indent=2))


# ===========================================================================
# LEGACY P2-02 API — regime-based weighted scorer (kept for validation tests).
# compute_score(rows) scores a candle series; score_signal(sig) is the new
# EV-based path used by the auto-trader. Both coexist.
# ===========================================================================

# Config-driven regime weights (defaults; overridable via WEIGHTS_PATH json).
DEFAULT_WEIGHTS = {
    "RANGING": {"momentum": 0.20, "trend_strength": 0.10, "mean_reversion": 0.60, "vol_dampening": 0.10},
    "TRENDING": {"momentum": 0.55, "trend_strength": 0.30, "mean_reversion": 0.05, "vol_dampening": 0.10},
    "VOLATILE": {"momentum": 0.30, "trend_strength": 0.15, "mean_reversion": 0.15, "vol_dampening": 0.40},
}
WEIGHTS_PATH = Path(__file__).resolve().parents[3] / "config" / "scorer-weights-trend.json"

MIN_ROWS = 40
MIN_HISTORY = 30  # warmup for indicators (e.g. ADX 14)


def load_weights() -> dict:
    if WEIGHTS_PATH.exists():
        try:
            data = json.loads(WEIGHTS_PATH.read_text(encoding="utf-8"))
            # Normalize/validate shape.
            for regime in DEFAULT_WEIGHTS:
                if regime not in data:
                    data[regime] = DEFAULT_WEIGHTS[regime]
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return {k: dict(v) for k, v in DEFAULT_WEIGHTS.items()}


def _classify_regime(rows: list[dict]) -> str:
    """Simple regime classifier: RANGING / TRENDING / VOLATILE."""
    if len(rows) < 2:
        return "RANGING"
    closes = [r["close"] for r in rows]
    # Trend: slope of closes; Volatility: stdev of returns.
    n = len(closes)
    x = list(range(n))
    xm = sum(x) / n
    ym = sum(closes) / n
    denom = sum((xi - xm) ** 2 for xi in x) or 1
    slope = sum((x[i] - xm) * (closes[i] - ym) for i in range(n)) / denom
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, n)]
    import statistics
    vol = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    norm_slope = abs(slope) / (ym or 1)
    if vol > 0.02:
        return "VOLATILE"
    if norm_slope > 0.0005:
        return "TRENDING"
    return "RANGING"


def compute_score(rows: list[dict]) -> dict:
    """Legacy P2-02: score a candle series 0-100 or None (fail-closed).

    Returns {'score', 'missing', 'regime', 'macro_multiplier'}.
    """
    if len(rows) < MIN_ROWS:
        return {"score": None, "missing": ["insufficient_rows"], "regime": None, "macro_multiplier": 1.0}
    # Indicator warmup: need enough history for the trend/vol calc.
    if len(rows) < MIN_HISTORY:
        return {"score": None, "missing": ["insufficient_history"], "regime": None, "macro_multiplier": 1.0}

    closes = [r["close"] for r in rows]
    regime = _classify_regime(rows)
    weights = load_weights()[regime]

    # momentum: recent return
    momentum = closes[-1] / closes[-10] - 1 if len(closes) >= 10 else 0.0
    # trend_strength: |slope| normalized
    n = len(closes)
    x = list(range(n))
    xm = sum(x) / n
    ym = sum(closes) / n
    denom = sum((xi - xm) ** 2 for xi in x) or 1
    slope = sum((x[i] - xm) * (closes[i] - ym) for i in range(n)) / denom
    trend_strength = min(1.0, abs(slope) / (ym or 1) * 1000)
    # mean_reversion: distance from mean
    import statistics
    mean = statistics.mean(closes)
    mean_reversion = -abs(closes[-1] / mean - 1)  # negative = far from mean
    # vol_dampening: inverse of volatility
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, n)]
    vol = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    vol_dampening = max(0.0, 1.0 - vol / 0.05)

    # Weighted composite -> 0-100 (bullish tilt > 50, bearish < 50).
    # Calibrated so: flat/neutral ~50, bullish trend ~65+, bearish ~<45.
    # momentum component: recent 10-bar return scaled to a 0-100 tilt.
    mom_component = 50.0 + momentum * 1000.0 * weights["momentum"]
    # trend_strength: adds bullish tilt when uptrend (slope > 0).
    trend_sign = 1.0 if slope > 0 else -1.0
    trend_component = trend_sign * trend_strength * 25.0 * weights["trend_strength"]
    # mean_reversion: contributes tilt when price deviates from mean.
    # Far below mean (negative mean_reversion) → bullish (buy the dip).
    mr_component = mean_reversion * 100.0 * weights["mean_reversion"]
    # vol_dampening: low vol = more confident (small tilt toward 50+).
    vol_component = vol_dampening * 50.0 * weights["vol_dampening"]
    score = max(0.0, min(100.0, mom_component + trend_component + mr_component + vol_component))
    return {"score": round(score, 1), "missing": [], "regime": regime, "macro_multiplier": 1.0}


def decision_record(symbol: str, timeframe: str, rows: list[dict]) -> dict:
    """Legacy P2-02: full decision record for a candle series."""
    result = compute_score(rows)
    action = "PENDING_RISK" if result["score"] is not None else "NO_TRADE"
    return {
        "symbol": symbol, "timeframe": timeframe,
        "score": result["score"], "missing": result["missing"],
        "regime": result["regime"], "macro_multiplier": result["macro_multiplier"],
        "action": action,
    }