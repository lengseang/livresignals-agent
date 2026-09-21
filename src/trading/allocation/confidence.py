"""P-A dual-track confidence router — evidence-based entry scoring.

Ranks each candidate entry 0..1 using ONLY the strategy's validated
conditions plus independent corroborating detectors. This is NOT a
prediction of per-trade profit ("sure win" does not exist); it selects
best-evidence entries for the tiny LIVE track while the paper track takes
every entry as the control group.

Components (weights in DUAL_TRACK_CONFIG):
  entry_zone     : drawdown <= -15% is the strategy's validated entry
  depth          : deeper dips (<= -25%) get more weight (buy MORE on fear)
  corroboration  : independent detectors firing alongside (vol spike,
                   momentum break, cross-div, deep dip, regime shift)
  regime         : bear/panic regimes historically = better accumulation
"""
from __future__ import annotations

import json
from pathlib import Path

from trading.allocation.engine import DEFAULT_DRAWDOWN_THRESHOLD

CONFIG_FILE = Path(__file__).resolve().parents[3] / "config" / "dual-track.json"


def _default_config() -> dict:
    return {
        "live_enabled": False,
        "live_conf_threshold": 0.60,
        "live_base_amount": 10.0,
        "live_max_per_entry": 50.0,
        "weights": {
            "entry_zone": 0.40,
            "depth": 0.30,
            "corroboration": 0.20,
            "regime": 0.10,
        },
        "regime_scores": {
            "BEAR": 0.8,
            "PANIC": 0.8,
            "RANGING": 0.5,
            "BULL": 0.3,
            "UNKNOWN": 0.5,
        },
    }


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            merged = _default_config()
            merged.update(cfg)
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return _default_config()


def live_enabled() -> bool:
    return bool(load_config().get("live_enabled", False))


def _component_entry_zone(dd: float, thr: float) -> float:
    if dd <= thr:
        return 1.0
    if dd <= -0.08:
        return 0.5
    return 0.0


def _component_depth(dd: float) -> float:
    if dd <= -0.25:
        return 1.0
    if dd <= -0.15:
        return 0.6
    if dd <= -0.08:
        return 0.2
    return 0.0


def _component_corroboration(detectors: list[str]) -> float:
    """Independent detectors firing beyond the entry condition itself."""
    extras = set(detectors) - {"dip_zone", "approach_zone"}
    n = len(extras)
    if n >= 2:
        return 1.0
    if n == 1:
        return 0.5
    return 0.0


def _component_regime(regime: str, regime_scores: dict) -> float:
    return regime_scores.get(regime, regime_scores.get("UNKNOWN", 0.5))


def compute_confidence(
    drawdown_pct: float,
    detectors: list[str] | None = None,
    regime: str = "UNKNOWN",
    cfg: dict | None = None,
) -> float:
    """Compute entry confidence 0..1 for a symbol.

    drawdown_pct: trailing 90d drawdown (fraction, e.g. -0.19)
    detectors   : signal types currently firing for this symbol
    regime      : regime label (BEAR/PANIC/RANGING/BULL/UNKNOWN)
    """
    cfg = cfg or load_config()
    weights = cfg["weights"]
    thr = cfg.get("drawdown_threshold", DEFAULT_DRAWDOWN_THRESHOLD)

    entry_zone = _component_entry_zone(drawdown_pct, thr)
    depth = _component_depth(drawdown_pct)
    corroboration = _component_corroboration(detectors or [])
    regime = _component_regime(regime, cfg["regime_scores"])

    conf = (
        weights["entry_zone"] * entry_zone
        + weights["depth"] * depth
        + weights["corroboration"] * corroboration
        + weights["regime"] * regime
    )
    return round(max(0.0, min(1.0, conf)), 4)


def should_route_live(confidence: float, cfg: dict | None = None) -> bool:
    """Route to LIVE only if the feature is on AND confidence clears the bar."""
    cfg = cfg or load_config()
    if not cfg.get("live_enabled", False):
        return False
    return confidence >= cfg.get("live_conf_threshold", 0.60)


def live_size(base_amount: float, cfg: dict | None = None) -> float:
    """Live entry size: small base, hard-capped at live_max_per_entry."""
    cfg = cfg or load_config()
    size = cfg.get("live_base_amount", 10.0)
    cap = cfg.get("live_max_per_entry", 50.0)
    # Never scale live size by multiplier — tiny + mechanical is the point.
    return round(min(size, cap), 2)
