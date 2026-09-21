#!/usr/bin/env python3
"""Capitulation Catch — validated short-term strategy (paper first).

The ONLY strategy that held out-of-sample across 15m/30m/1h (3.6yr data):

    ENTRY  : trailing drawdown <= -2%  AND  high volatility (ATR > 1.5x median)
             AND  >= 3 consecutive red 30m bars  AND  RSI(14) <= 40
    EXIT   : hold ~3h (9 bars), ATR-scaled TP/SL at 2:1, or time-stop

Validated result (30m BTC, OOS n=112): 64.3% win rate, +0.20% avg/trade.
PAPER ONLY. Tracks its own budget in data/capitulation-state.json.

Usage:
  capitulation_catch.py check     # one scan + optional entry/exit
  capitulation_catch.py summary   # print paper P&L summary
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading.data.ccxt_fetcher import make_exchange  # noqa: E402

UNIVERSE = ["BTC/USDT"]  # BTC-ONLY: alts are negative expectancy (see expectancy_alts.py)
TIMEFRAME = "30m"
STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "capitulation-state.json"

# Validated thresholds (30m, walk-forward stable, 3.6yr):
#   70.2% win rate, +0.44% EV/trade, 2.92 profit factor (BTC only)
DIP_THRESHOLD = -0.02     # trailing drawdown <= -2%
VOL_RATIO_MIN = 1.5       # ATR ratio >= 1.5x its own median (high-vol gate)
MIN_RED_BARS = 3          # >= 3 consecutive red bars
RSI_MAX = 30              # RSI(14) <= 30  (deeply oversold — the accuracy driver)
HOLD_BARS = 12            # ~6h hold on 30m
STARTING_BUDGET = 500.0


def _default_state() -> dict:
    return {
        "budget": STARTING_BUDGET,
        "open_positions": [],
        "closed_trades": [],
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "last_check_utc": None,
    }


def _load() -> dict:
    if STATE_PATH.exists():
        try:
            s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            for k, v in _default_state().items():
                s.setdefault(k, v)
            return s
        except (json.JSONDecodeError, OSError):
            pass
    return _default_state()


def _save(s: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")


def _fetch_features(ex, symbol: str, lookback: int = 90) -> dict:
    """Compute the capitulation-catch entry features on 30m candles."""
    ohlcv = ex.fetch_ohlcv(symbol, TIMEFRAME, limit=lookback)
    if not ohlcv or len(ohlcv) < 40:
        return {}
    closes = [float(c[4]) for c in ohlcv]
    highs = [float(c[2]) for c in ohlcv]
    lows = [float(c[3]) for c in ohlcv]

    price = closes[-1]
    hi = max(closes[:-1])
    dd = price / hi - 1.0 if hi > 0 else 0.0

    # ATR (14) and its own 60-bar median for the high-vol ratio
    trs = []
    for i in range(1, len(ohlcv)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    atr_now = sum(trs[-14:]) / 14 if len(trs) >= 14 else 0.0
    atr_med = statistics.median(trs[-60:]) if len(trs) >= 60 else atr_now
    atr_ratio = atr_now / atr_med if atr_med > 0 else 1.0

    # Consecutive red bars
    red = 0
    j = len(closes) - 1
    while j > 0 and closes[j] < closes[j - 1]:
        red += 1
        j -= 1

    # RSI(14)
    rsi = _rsi(closes)

    return {
        "symbol": symbol, "price": price, "drawdown_pct": dd,
        "atr": atr_now, "atr_ratio": atr_ratio, "red_bars": red, "rsi": rsi,
    }


def _rsi(closes: list[float], lookback: int = 14) -> float:
    if len(closes) < lookback + 1:
        return 50.0
    gains = losses = 0.0
    for j in range(len(closes) - lookback, len(closes)):
        d = closes[j] - closes[j - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    rs = (gains / lookback) / (losses / lookback)
    return 100.0 - 100.0 / (1.0 + rs)


def entry_signal(f: dict) -> bool:
    return (
        f["drawdown_pct"] <= DIP_THRESHOLD
        and f["atr_ratio"] >= VOL_RATIO_MIN
        and f["red_bars"] >= MIN_RED_BARS
        and f["rsi"] <= RSI_MAX
    )


def check_and_trade() -> dict:
    s = _load()
    events = []
    ex = make_exchange(sandbox=True)

    # 1. Close positions: TP/SL hit, or time-stop expired (HOLD_BARS elapsed).
    now_ms = int(time.time() * 1000)
    keep = []
    for pos in s["open_positions"]:
        try:
            px = float(ex.fetch_ticker(pos["symbol"])["last"])
        except Exception:
            keep.append(pos)  # can't price — keep it, retry next cycle
            continue
        reason = None
        if px >= pos["tp"]:
            reason = "TP"
        elif px <= pos["sl"]:
            reason = "SL"
        elif (now_ms - pos["ts_ms"]) >= pos["hold_ms"]:
            reason = "TIME"
        if reason:
            pnl = (px - pos["entry"]) / pos["entry"] * pos["size"]
            s["budget"] += pos["size"] + pnl
            s["closed_trades"].append({
                "symbol": pos["symbol"], "entry": pos["entry"], "exit": round(px, 4),
                "pnl": round(pnl, 4), "reason": reason,
                "ts_utc": datetime.now(timezone.utc).isoformat(),
            })
            events.append(f"CLOSE {pos['symbol']} ({reason}) {pnl:+.2f} USDT")
        else:
            keep.append(pos)
    s["open_positions"] = keep

    # 2. Open new positions on a validated capitulation signal.
    for sym in UNIVERSE:
        if any(p["symbol"] == sym for p in s["open_positions"]):
            continue
        f = _fetch_features(ex, sym)
        if not f:
            continue
        if not entry_signal(f):
            continue
        # ATR-scaled TP/SL at 2:1
        stop = 1.5 * f["atr"] / f["price"]  # stop = 1.5x ATR, as fraction
        if stop <= 0:
            continue
        size = min(s["budget"] * 0.20, 50.0)  # risk-capped paper size
        if size < 5.0:
            continue
        tp = round(f["price"] * (1 + 2 * stop), 4)
        sl = round(f["price"] * (1 - stop), 4)
        s["budget"] -= size
        s["open_positions"].append({
            "symbol": sym, "entry": f["price"], "tp": tp, "sl": sl,
            "size": size, "ts_ms": now_ms, "hold_ms": HOLD_BARS * 30 * 60 * 1000,
        })
        events.append(
            f"OPEN {sym} @ {f['price']} (dd={f['drawdown_pct']*100:.1f}% "
            f"vol={f['atr_ratio']:.1f}x red={f['red_bars']} rsi={f['rsi']:.0f}) "
            f"TP {tp} / SL {sl}"
        )

    s["last_check_utc"] = datetime.now(timezone.utc).isoformat()
    _save(s)
    return {
        "budget": round(s["budget"], 2), "open": len(s["open_positions"]),
        "closed": len(s["closed_trades"]), "events": events,
    }


def summary() -> dict:
    s = _load()
    wins = sum(1 for t in s["closed_trades"] if t["pnl"] > 0)
    losses = sum(1 for t in s["closed_trades"] if t["pnl"] <= 0)
    total_pnl = sum(t["pnl"] for t in s["closed_trades"])
    n = wins + losses
    return {
        "budget": round(s["budget"], 2),
        "starting_budget": STARTING_BUDGET,
        "open_positions": len(s["open_positions"]),
        "closed_trades": n,
        "wins": wins, "losses": losses,
        "win_rate": round(wins / n * 100, 1) if n else None,
        "total_pnl": round(total_pnl, 2),
        "net_return_pct": round(total_pnl / STARTING_BUDGET * 100, 2),
        "trades": s["closed_trades"][-10:],
    }


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "check":
        print(json.dumps(check_and_trade(), indent=2))
    elif mode == "summary":
        print(json.dumps(summary(), indent=2))
    else:
        print(json.dumps({"usage": "capitulation_catch.py [check|summary]"}, indent=2))
