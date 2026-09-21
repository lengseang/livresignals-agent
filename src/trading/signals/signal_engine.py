"""Signal engine: generates structured trading signals from live market data.

A signal is a directional/actionable observation with a resolution horizon.
The engine checks each symbol in the universe and emits a signal record that
gets logged, broadcast, and later scored for accuracy.

Signal fields (stable schema — used by ledger, accuracy, and the UI):
  id            : unique (symbol + timestamp bucket)
  ts_utc        : generation time
  symbol        : BTC/USDT | ETH/USDT | SOL/USDT
  action        : BUY | HOLD | WATCH (accumulation strategy: dip-buy bias)
  bias          : BULLISH | BEARISH | NEUTRAL (direction of the signal)
  price         : reference price at signal time
  drawdown_pct  : distance from 90d high (the strategy's core trigger)
  regime        : RANGING | TRENDING | VOLATILE (regime classifier)
  score         : P2 scorer 0-100 (if computable)
  horizon_h     : hours until the signal is scored (e.g. 24, 72, 168)
  predicted     : expected price change direction over horizon
  reason        : human-readable driver
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from trading.allocation.engine import AllocationConfig
from trading.data.ccxt_fetcher import make_exchange
from trading.signals.regime import classify_regime

SIGNAL_LEDGER = Path(__file__).resolve().parents[3] / "data" / "signal-ledger.json"
UNIVERSE = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
HORIZONS = [24, 72, 168]  # score at 1d, 3d, 7d


def _load_ledger() -> dict:
    if SIGNAL_LEDGER.exists():
        try:
            return json.loads(SIGNAL_LEDGER.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"signals": [], "started_at_utc": datetime.now(timezone.utc).isoformat()}


def _save_ledger(ledger: dict) -> None:
    SIGNAL_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_LEDGER.write_text(json.dumps(ledger, indent=2), encoding="utf-8")


def market_snapshot(symbol: str, lookback_days: int = 90) -> dict:
    """Live OHLCV-derived features: drawdown, regime, recent momentum."""
    ex = make_exchange(sandbox=True)
    since = int((time.time() - lookback_days * 86400) * 1000)
    ohlcv = ex.fetch_ohlcv(symbol, "1d", since=since, limit=lookback_days + 1)
    if not ohlcv:
        raise RuntimeError(f"no data for {symbol}")
    closes = [float(c[4]) for c in ohlcv[:-1]]
    current = float(ohlcv[-1][4])
    hi = max(closes) if closes else current
    dd = current / hi - 1.0 if hi > 0 else 0.0

    rows = [
        {"open": c[1], "high": c[2], "low": c[3], "close": c[4],
         "volume": c[5], "open_time_utc": int(c[0])}
        for c in ohlcv
    ]
    try:
        regime = classify_regime(rows)
    except Exception:  # noqa: BLE001
        regime = "RANGING"
    return {"price": current, "drawdown_pct": dd, "regime": regime, "high_90d": hi}


def generate_signal(symbol: str, cfg: AllocationConfig | None = None) -> dict:
    """Generate one signal record for a symbol from live data."""
    cfg = cfg or AllocationConfig()
    snap = market_snapshot(symbol, cfg.lookback_days)
    dd = snap["drawdown_pct"]
    in_dip = dd <= cfg.drawdown_threshold

    # Accumulation bias: dip-zone = accumulation opportunity (BULLISH bias),
    # extended = caution (BEARISH bias), otherwise NEUTRAL watch.
    if in_dip:
        action, bias = "BUY", "BULLISH"
        reason = f"dip zone (dd {dd*100:.1f}% <= {cfg.drawdown_threshold*100:.0f}%) — accumulate at 1.5x"
    elif dd <= -0.08:
        action, bias = "WATCH", "NEUTRAL"
        reason = f"approaching dip zone (dd {dd*100:.1f}%) — watch for entry"
    else:
        action, bias = "WATCH", "BEARISH"
        reason = f"extended (dd {dd*100:.1f}%) — no accumulation, hold cash"

    # Regime informs bias strength.
    if snap["regime"] == "TRENDING" and dd < 0:
        bias = "BULLISH" if not in_dip else bias
        reason += f" | TRENDING regime favors momentum"

    ts_ms = int(time.time() * 1000)
    signal = {
        "id": f"{symbol.replace('/','')}-{ts_ms}",
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "ts_ms": ts_ms,
        "symbol": symbol,
        "action": action,
        "bias": bias,
        "price": round(snap["price"], 2),
        "drawdown_pct": round(dd, 4),
        "regime": snap["regime"],
        "horizon_h": HORIZONS[0],
        "predicted": "up" if bias == "BULLISH" else ("down" if bias == "BEARISH" else "flat"),
        "reason": reason,
        "outcome": None,  # filled when horizon resolves
    }
    return signal


def run_signal_cycle(cfg: AllocationConfig | None = None) -> list[dict]:
    """Generate + persist signals for the whole universe. Returns new signals."""
    ledger = _load_ledger()
    now_ms = int(time.time() * 1000)
    new_signals = []
    for sym in UNIVERSE:
        try:
            sig = generate_signal(sym, cfg)
        except Exception as e:  # noqa: BLE001
            # Keep the loop alive per-symbol; log the failure.
            sig = {
                "id": f"{sym.replace('/','')}-{now_ms}", "ts_utc": datetime.now(timezone.utc).isoformat(),
                "ts_ms": now_ms, "symbol": sym, "action": "ERROR", "bias": "NEUTRAL",
                "price": None, "drawdown_pct": None, "regime": "?",
                "horizon_h": HORIZONS[0], "predicted": "flat",
                "reason": f"engine error: {str(e)[:120]}", "outcome": None,
            }
        ledger["signals"].append(sig)
        new_signals.append(sig)
    _save_ledger(ledger)
    return new_signals


def resolve_outcomes(ledger: dict | None = None, prices: dict[str, float] | None = None) -> int:
    """Score signals whose horizon has elapsed. Returns count resolved."""
    ledger = ledger or _load_ledger()
    if prices is None:
        ex = make_exchange(sandbox=True)
        prices = {}
        for sym in UNIVERSE:
            try:
                prices[sym] = float(ex.fetch_ticker(sym)["last"])
            except Exception:  # noqa: BLE001
                pass
    now_ms = int(time.time() * 1000)
    resolved = 0
    for sig in ledger["signals"]:
        if sig.get("outcome") is not None:
            continue
        if sig.get("ts_ms") is None or sig.get("horizon_h") is None:
            continue
        if now_ms - sig["ts_ms"] < sig["horizon_h"] * 3600 * 1000:
            continue
        price0 = sig.get("price")
        price1 = prices.get(sig["symbol"])
        if not price0 or not price1:
            continue
        actual = price1 / price0 - 1
        pred = sig.get("predicted")
        if pred == "up":
            correct = actual > 0
        elif pred == "down":
            correct = actual < 0
        else:
            correct = abs(actual) < 0.005  # flat: within 0.5%
        sig["outcome"] = {
            "correct": correct,
            "actual_pct": round(actual * 100, 3),
            "resolved_at_utc": datetime.now(timezone.utc).isoformat(),
            "price1": price1,
        }
        # Feed the resolution into the EV scorer's training set (learning loop).
        try:
            from trading.signals.scorer import record_resolution
            record_resolution(sig, correct)
        except Exception:  # noqa: BLE001
            pass  # scorer training is best-effort; never block resolution
        resolved += 1
    _save_ledger(ledger)
    # After resolving, retrain the model if enough rows accumulated.
    if resolved:
        try:
            from trading.signals.scorer import retrain
            retrain()
        except Exception:  # noqa: BLE001
            pass
    return resolved


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "cycle"
    if mode == "cycle":
        new = run_signal_cycle()
        print(json.dumps(new, indent=2))
        print(f"TOTAL LEDGER: {len(_load_ledger()['signals'])}")
    elif mode == "resolve":
        n = resolve_outcomes()
        print(f"resolved {n} signals")
    elif mode == "ledger":
        print(json.dumps(_load_ledger(), indent=2)[:1500])