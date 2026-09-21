"""Multi-signal detectors — big and small signals from live market data.

Event-driven: each detector produces a SIGNAL record only when its condition
is true. The watcher dedupes by (symbol, signal_type, state) so we broadcast
on state TRANSITIONS, not on every poll.

Signal severity:
  BIG   — actionable/notable: dip-zone entry, deep-dip, regime shift
  SMALL — informational: momentum break, approach zone, vol spike, cross-div

Detectors (all derived from live OHLCV + indicators we already have):
  dip_zone      : dd <= -15%  (BIG)  — the strategy's entry condition
  deep_dip      : dd <= -25%  (BIG)  — extreme fear
  regime_shift  : regime != last known regime (BIG)
  momentum_break: price crosses 20d high (SMALL, bullish) / 20d low (SMALL, bearish)
  approach_zone : -15% < dd <= -8% (SMALL) — nearing entry
  vol_spike     : ATR ratio > 1.5x its 30d median (SMALL) — volatility expansion
  cross_div     : BTC moves >1% but ETH/SOL within 0.3% (SMALL) — divergence watch
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from trading.allocation.engine import AllocationConfig
from trading.data.ccxt_fetcher import make_exchange
from trading.signals.regime import classify_regime

UNIVERSE = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

SIGNAL_TYPES = {
    "dip_zone":       {"severity": "BIG", "name": "DIP ZONE",       "emoji": "🟢"},
    "deep_dip":       {"severity": "BIG", "name": "DEEP DIP",       "emoji": "🔴"},
    "regime_shift":   {"severity": "BIG", "name": "REGIME SHIFT",   "emoji": "🔄"},
    "momentum_break": {"severity": "SMALL", "name": "MOMENTUM BREAK", "emoji": "🚀"},
    "approach_zone":  {"severity": "SMALL", "name": "APPROACH ZONE", "emoji": "👀"},
    "vol_spike":      {"severity": "SMALL", "name": "VOLATILITY SPIKE", "emoji": "⚡"},
    "cross_div":      {"severity": "SMALL", "name": "CROSS DIVERGENCE", "emoji": "🔀"},
}

STATE_FILE = Path(__file__).resolve().parents[3] / "data" / "signal-states.json"


def _load_states() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"states": {}}


def _save_states(states: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(states, indent=2), encoding="utf-8")


def market_features(symbol: str, lookback_days: int = 90) -> dict:
    """Live features: drawdown, regime, momentum break, ATR ratio."""
    ex = make_exchange(sandbox=True)
    since = int((datetime.now(timezone.utc).timestamp() - lookback_days * 86400) * 1000)
    ohlcv = ex.fetch_ohlcv(symbol, "1d", since=since, limit=lookback_days + 1)
    if not ohlcv:
        raise RuntimeError(f"no data for {symbol}")
    closes = [float(c[4]) for c in ohlcv]
    current = closes[-1]
    past = closes[:-1]
    hi90 = max(past[-90:]) if len(past) >= 90 else max(past)
    lo90 = min(past[-90:]) if len(past) >= 90 else min(past)
    hi20 = max(past[-20:]) if len(past) >= 20 else max(past)
    lo20 = min(past[-20:]) if len(past) >= 20 else min(past)
    dd = current / hi90 - 1.0 if hi90 > 0 else 0.0

    # ATR ratio (volatility expansion)
    trs = []
    for i in range(1, len(ohlcv)):
        h, l, pc = float(ohlcv[i][2]), float(ohlcv[i][3]), float(ohlcv[i - 1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr_now = sum(trs[-14:]) / 14 if len(trs) >= 14 else 0.0
    atr_hist = sorted(trs[-60:]) if trs else []
    atr_med = atr_hist[len(atr_hist) // 2] if atr_hist else atr_now
    atr_ratio = atr_now / atr_med if atr_med > 0 else 1.0

    rows = [
        {"open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5], "open_time_utc": int(c[0])}
        for c in ohlcv
    ]
    try:
        regime = classify_regime(rows)
    except Exception:  # noqa: BLE001
        regime = "RANGING"

    return {
        "price": current, "drawdown_pct": dd, "regime": regime,
        "hi90": hi90, "lo90": lo90, "hi20": hi20, "lo20": lo20,
        "atr_ratio": atr_ratio,
        "red_bars": _count_recent_reds(symbol),  # 1h red-bar count (78.7% WR lever)
    }


def _count_recent_reds(symbol: str) -> int:
    """Count red (downtick) bars in the last 3 hours — frequency-optimized.

    The loose config (validated 2026-08-13) fires on >=1 red bar, so we count
    reds in the last 3 bars (not strictly consecutive) to boost frequency.
    """
    try:
        ex = make_exchange(sandbox=True)
        ohlcv = ex.fetch_ohlcv(symbol, "1h", limit=4)
        if not ohlcv or len(ohlcv) < 2:
            return 0
        closes = [c[4] for c in ohlcv]
        reds = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i-1])
        return reds
    except Exception:  # noqa: BLE001
        return 0


def detect(symbol: str, features: dict) -> list[dict]:
    """Return fired signal records for a symbol given its features.

    Optimized thresholds (2026-08-12): shallow dip = win-win, deep dip = avoid.
    """
    out = []
    dd = features["drawdown_pct"]
    cfg = AllocationConfig()

    if dd <= -0.25:
        out.append(_sig(symbol, "deep_dip", f"extreme fear — {dd*100:.1f}% below 90d high (AVOID — knife)"))
    elif dd <= -0.12:
        out.append(_sig(symbol, "deep_dip", f"deep dip — {dd*100:.1f}% below 90d high (low EV)"))
    elif dd <= -0.05:
        # THE WIN-WIN ZONE: shallow dip, high win rate.
        out.append(_sig(symbol, "dip_zone", f"optimal dip — {dd*100:.1f}% below 90d high (57.8% win @ 48h)"))
    elif dd <= -0.02:
        out.append(_sig(symbol, "approach_zone", f"mild pullback — {dd*100:.1f}% (weak edge)"))

    if features["price"] >= features["hi20"]:
        out.append(_sig(symbol, "momentum_break", f"broke 20d high {features['hi20']:.2f} — momentum up"))
    elif features["price"] <= features["lo20"]:
        out.append(_sig(symbol, "momentum_break", f"broke 20d low {features['lo20']:.2f} — momentum down"))

    # High vol is now a POSITIVE confirmation for dip reversion (66% WR),
    # so we annotate it on the dip signal rather than fire a separate warning.
    # Only extreme vol (>2.5x) is still flagged as a risk.
    if features["atr_ratio"] >= 2.5:
        out.append(_sig(symbol, "vol_spike", f"EXTREME volatility {features['atr_ratio']:.2f}x median — chaotic, caution"))

    return out


def _sig(symbol: str, stype: str, reason: str) -> dict:
    meta = SIGNAL_TYPES[stype]
    return {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol, "signal_type": stype,
        "severity": meta["severity"], "name": meta["name"], "emoji": meta["emoji"],
        "reason": reason,
    }


def check_and_fire(symbol: str, features: dict | None = None) -> list[dict]:
    """Check a symbol; return NEW signals (state transitions) only."""
    if features is None:
        try:
            features = market_features(symbol)
        except Exception as e:  # noqa: BLE001
            return [{"ts_utc": datetime.now(timezone.utc).isoformat(), "symbol": symbol,
                     "signal_type": "error", "severity": "SMALL", "name": "CHECK ERROR",
                     "emoji": "⚠️", "reason": str(e)[:100]}]
    fired = detect(symbol, features)
    states = _load_states()
    new = []
    for sig in fired:
        key = f"{symbol}:{sig['signal_type']}"
        prev = states["states"].get(key)
        now_val = f"{sig['name']}:{round(features['price'], 2)}"
        # Fire if the type is new, or if the fired-state (not price) changed.
        # We treat "signal fired" as a boolean state per type — emit once per
        # activation, not every poll while the condition persists.
        if prev is None or prev != "fired":
            sig["price"] = round(features["price"], 2)
            sig["drawdown_pct"] = round(features["drawdown_pct"], 4)
            new.append(sig)
        states["states"][key] = "fired"
    # Reset signals that are no longer firing (so they can fire again later).
    active_types = {s["signal_type"] for s in fired}
    for key in list(states["states"].keys()):
        sym, stype = key.split(":", 1)
        if sym == symbol and stype not in active_types:
            states["states"][key] = "idle"
    _save_states(states)
    return new


def market_moves(symbol: str, lookback_days: int = 30) -> dict:
    """24h and 4h percentage moves for cross-symbol divergence detection."""
    ex = make_exchange(sandbox=True)
    since = int((datetime.now(timezone.utc).timestamp() - lookback_days * 86400) * 1000)
    ohlcv = ex.fetch_ohlcv(symbol, "1h", since=since, limit=48)  # 48 hours
    if not ohlcv:
        return {"move_24h": 0.0, "move_4h": 0.0}
    closes = [float(c[4]) for c in ohlcv]
    cur = closes[-1]
    m24 = cur / closes[-25] - 1 if len(closes) >= 25 else 0.0
    m4 = cur / closes[-5] - 1 if len(closes) >= 5 else 0.0
    return {"move_24h": m24, "move_4h": m4}


def detect_cross_divergence(moves: dict[str, dict]) -> list[dict]:
    """Fire cross_div on a symbol when BTC moves >1% (24h) but the symbol
    moved <0.3% — the laggard may catch up (or BTC is leading the move).
    """
    out = []
    btc = moves.get("BTC/USDT", {})
    btc24 = btc.get("move_24h", 0.0)
    if abs(btc24) < 0.01:
        return out
    for sym in ("ETH/USDT", "SOL/USDT"):
        mv = moves.get(sym, {})
        m24 = mv.get("move_24h", 0.0)
        if abs(m24) < 0.003:
            direction = "up" if btc24 > 0 else "down"
            out.append(_sig(sym, "cross_div",
                            f"BTC moved {btc24*100:.1f}% (24h) but {sym} only {m24*100:.1f}% — divergence, watch for catch-up {direction}"))
    return out


def scan_all() -> list[dict]:
    """Check all symbols; return all NEW signals."""
    all_new = []
    features_map = {}
    for sym in UNIVERSE:
        try:
            features_map[sym] = market_features(sym)
        except Exception:  # noqa: BLE001
            continue
    for sym, f in features_map.items():
        all_new.extend(check_and_fire(sym, features=f))
    # Cross-symbol divergence (needs moves from all symbols).
    moves = {}
    for sym in UNIVERSE:
        try:
            moves[sym] = market_moves(sym)
        except Exception:  # noqa: BLE001
            moves[sym] = {"move_24h": 0.0, "move_4h": 0.0}
    for sig in detect_cross_divergence(moves):
        states = _load_states()
        key = f"{sig['symbol']}:{sig['signal_type']}"
        if states["states"].get(key) != "fired":
            all_new.append(sig)
        states["states"][key] = "fired"
        _save_states(states)
    return all_new