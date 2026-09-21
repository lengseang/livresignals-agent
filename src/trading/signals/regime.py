"""Regime classifier: RANGING | TRENDING | VOLATILE with hysteresis.

Per docs/spec/p2-01-indicators-regime.md:
- TRENDING: ADX(14) >= 25 AND |EMA20-EMA50|/EMA50 >= 1.5%.
- VOLATILE: ATR(14)/close >= 1.2 * trailing ATR ratio OR realized vol >= p90.
- RANGING: fallback, always defined.
- Overlap precedence: TRENDING > VOLATILE > RANGING.
- Hysteresis: a regime switch only commits after 2 consecutive closed candles.
"""
from __future__ import annotations

from trading.signals.indicators import adx, atr, ema, realized_vol

REGIMES = ("RANGING", "TRENDING", "VOLATILE")
PRECEDENCE = {"TRENDING": 3, "VOLATILE": 2, "RANGING": 1}

# Canonical thresholds (config-like constants, spec §5).
ADX_TREND_MIN = 25.0
EMA_SEPARATION_MIN = 0.015
ATR_VOLATILE_RATIO = 1.2
RV_PERCENTILE = 0.90
HYSTERESIS_CANDLES = 2


def _classify_single(rows: list[dict]) -> str:
    """Deprecated placeholder — superseded by _classify_single_fixed.

    Kept only as documentation of the naive v1 approach; the fixed version
    compares ATR ratio against its trailing median, which is the canonical rule.
    """
    return _classify_single_fixed(rows)


def _classify_single_fixed(rows: list[dict]) -> str:
    """Fixed volatility comparison: ATR/close vs rolling median ATR/close ratio."""
    closes = [float(r["close"]) for r in rows]
    if len(closes) < 60:
        return "RANGING"
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    if not e20 or not e50 or e50[-1] <= 0:
        return "RANGING"
    ax = adx(rows, 14)
    a = atr(rows, 14)
    rv = realized_vol(rows, 20)
    if not ax or not a or not rv:
        return "RANGING"
    adx14, atr14, close, rv20 = ax[-1], a[-1], closes[-1], rv[-1]
    ema20, ema50 = e20[-1], e50[-1]

    # Volatility: ATR ratio vs its trailing median; or realized vol vs median ratio.
    atr_ratios = [x / closes[-1 - (len(a) - 1 - i)] if closes[-1 - (len(a) - 1 - i)] else 0 for i, x in enumerate(a)]
    med = sorted(atr_ratios)[len(atr_ratios) // 2] if atr_ratios else 0.0
    atr_ratio = atr14 / close if close > 0 else 0.0
    rv_med = sorted(rv)[len(rv) // 2] if rv else 0.0
    volatile = (med > 0 and atr_ratio >= ATR_VOLATILE_RATIO * med) or (
        rv_med > 0 and rv20 >= ATR_VOLATILE_RATIO * rv_med
    )

    trending = adx14 >= ADX_TREND_MIN and abs(ema20 - ema50) / ema50 >= EMA_SEPARATION_MIN
    if trending:
        return "TRENDING"  # precedence over volatile per spec
    if volatile:
        return "VOLATILE"
    return "RANGING"


def classify_regime(rows: list[dict]) -> str:
    """Classify using the fixed volatility logic (canonical precedence)."""
    return _classify_single_fixed(rows)


def classify_with_hysteresis(rows: list[dict], prev_regime: str | None = None) -> str:
    """Classify the last candle, requiring 2 consecutive candles for a switch.

    Scans back from the end; a candidate regime must appear in the last
    HYSTERESIS_CANDLES classified windows (using progressively truncated rows).
    """
    if len(rows) < 60:
        return "RANGING"
    if prev_regime is None:
        return classify_regime(rows)

    # Evaluate the last N windows to detect persistence.
    labels: list[str] = []
    for back in range(HYSTERESIS_CANDLES):
        window = rows[: len(rows) - back]
        if len(window) < 60:
            labels.append(prev_regime)
        else:
            labels.append(_classify_single_fixed(window))

    current = labels[0]
    if current == prev_regime:
        return prev_regime
    # Switch only if all hysteresis windows agree on the new regime.
    if all(l == current for l in labels):
        return current
    return prev_regime


def regime_for_rows(rows: list[dict], prev_regime: str | None = None) -> str:
    return classify_with_hysteresis(rows, prev_regime)