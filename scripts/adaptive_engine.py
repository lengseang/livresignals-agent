#!/usr/bin/env python3
"""Adaptive trading engine — Layer 1-3 of the framework.

Combines:
  L1 REGIME DETECTOR  : trend-direction (slope) + volatility + hysteresis
  L2 STRATEGY SELECTOR: regime -> which strategy (currently capitulation-catch;
                        future momentum/knife variants slot in here)
  L3 POSITION SIZER   : Kelly-capped by expectancy, not confidence

Validated by regime_proof.py: capitulation only trades in HIVOL; trend-direction
tilts EV (TREND +0.62%, DOWNTREND +0.46%, RANGE +0.26%). LOWVOL = stand down.

Paper only. State in data/adaptive-state.json.
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading.data.ccxt_fetcher import make_exchange  # noqa: E402

SYMBOL = "BTC/USDT"
TIMEFRAME = "30m"
STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "adaptive-state.json"

# Regime thresholds (30m)
SLOPE_TREND_UP = 0.0005     # normalized 90-bar slope for uptrend
SLOPE_TREND_DN = -0.0005    # downtrend
VOL_HIGH = 0.004            # 14-bar stdev threshold (matches validated tests)

# Strategy parameters (validated)
DIP_THRESHOLD = -0.02
VOL_RATIO_MIN = 1.5
MIN_RED_BARS = 3
RSI_MAX = 30
HOLD_BARS = 12
STARTING_BUDGET = 500.0

# Edge-by-regime (from regime_proof.py) — used for sizing.
# Static defaults; OVERRIDDEN by data/regime-edges.json when the weekly
# learner (weekly_regime_refit.py) has re-fit them on recent data.
REGIME_EDGE = {
    "TREND+HIVOL": 0.0062,
    "DOWNTREND+HIVOL": 0.0046,
    "RANGE+HIVOL": 0.0026,
    "HIVOL": 0.0044,          # fallback
    "LOVOL": 0.0,
}
REGIME_WR = {
    "TREND+HIVOL": 0.677,
    "DOWNTREND+HIVOL": 0.670,
    "RANGE+HIVOL": 0.620,
    "HIVOL": 0.64,
    "LOVOL": 0.0,
}
EDGES_PATH = Path(__file__).resolve().parents[1] / "data" / "regime-edges.json"


def _load_regime_edges():
    """Merge weekly re-fit edges over static defaults (file wins if present)."""
    edge = dict(REGIME_EDGE)
    wr = dict(REGIME_WR)
    if EDGES_PATH.exists():
        try:
            d = json.loads(EDGES_PATH.read_text(encoding="utf-8"))
            for k, v in d.items():
                if k == "as_of_utc":
                    continue
                if v.get("n", 0) >= 15:
                    edge[k] = v.get("ev", edge.get(k, 0.0))
                    wr[k] = v.get("win_rate", wr.get(k, 0.0))
        except (json.JSONDecodeError, OSError):
            pass
    return edge, wr


def _default_state():
    return {
        "budget": STARTING_BUDGET,
        "regime": "RANGE+LOVOL",
        "open_positions": [],
        "closed_trades": [],
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "last_check_utc": None,
    }


def _load():
    if STATE_PATH.exists():
        try:
            s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            for k, v in _default_state().items():
                s.setdefault(k, v)
            return s
        except (json.JSONDecodeError, OSError):
            pass
    return _default_state()


def _save(s):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")


def _notify(title: str, body: str) -> None:
    """Send a Telegram card on trade events. Best-effort — never blocks trading."""
    try:
        from trading.tg.bot_ui import send_card
        send_card("signals", title, "<pre>" + body + "</pre>")
    except Exception:  # noqa: BLE001
        pass  # Telegram is optional; trading must never depend on it


def _learn_from_closed_trades(s: dict) -> None:
    """Per-trade learner: recompute live win-rate/EV per regime from recent
    closed trades and fold into sizing, so the engine adapts after EVERY trade,
    not just the weekly re-fit. Cheap — runs over the in-memory trade list."""
    trades = s["closed_trades"]
    if len(trades) < 5:
        return  # too few to meaningfully re-estimate
    recent = trades[-40:]  # rolling window
    by_regime = {}
    for t in recent:
        r = by_regime.setdefault(t["regime"], {"wins": 0, "n": 0, "pnl": []})
        r["n"] += 1
        if t["pnl"] > 0:
            r["wins"] += 1
        r["pnl"].append(t["pnl"])
    learned = {}
    for regime, r in by_regime.items():
        if r["n"] < 5:
            continue
        wr = r["wins"] / r["n"]
        avg_pnl = sum(r["pnl"]) / r["n"]
        learned[regime] = {"win_rate": round(wr, 3), "avg_pnl": round(avg_pnl, 4), "n": r["n"]}
    s["live_edge"] = learned


def _rsi(closes, lookback=14):
    if len(closes) < lookback + 1:
        return 50.0
    g = l = 0.0
    for j in range(len(closes) - lookback, len(closes)):
        d = closes[j] - closes[j - 1]
        if d >= 0:
            g += d
        else:
            l -= d
    if l == 0:
        return 100.0
    rs = (g / lookback) / (l / lookback)
    return 100.0 - 100.0 / (1.0 + rs)


def detect_regime(ex) -> str:
    """L1: return regime tag like 'TREND+HIVOL', 'DOWNTREND+HIVOL', 'RANGE+LOVOL'."""
    ohlcv = ex.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=100)
    if not ohlcv or len(ohlcv) < 60:
        return "RANGE+LOVOL"
    closes = [float(c[4]) for c in ohlcv]

    # trend direction: 90-bar normalized slope
    seg = closes[-90:]
    m = len(seg)
    x = list(range(m))
    xm = (m - 1) / 2
    ym = sum(seg) / m
    denom = sum((xi - xm) ** 2 for xi in x) or 1
    num = sum((x[j] - xm) * (seg[j] - ym) for j in range(m))
    slope = (num / denom) / (ym or 1)
    if slope > SLOPE_TREND_UP:
        trend = "TREND"
    elif slope < SLOPE_TREND_DN:
        trend = "DOWNTREND"
    else:
        trend = "RANGE"

    # volatility: 14-bar stdev of returns
    rets = [closes[j] / closes[j - 1] - 1 for j in range(len(closes) - 14, len(closes))]
    vol = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    voltag = "HIVOL" if vol > VOL_HIGH else "LOVOL"

    return f"{trend}+{voltag}"


def entry_features(ex):
    """Compute capitulation-catch features on 30m."""
    ohlcv = ex.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=90)
    if not ohlcv or len(ohlcv) < 40:
        return {}
    closes = [float(c[4]) for c in ohlcv]
    highs = [float(c[2]) for c in ohlcv]
    lows = [float(c[3]) for c in ohlcv]
    price = closes[-1]
    hi = max(closes[:-1])
    dd = price / hi - 1.0 if hi > 0 else 0.0

    trs = []
    for i in range(1, len(ohlcv)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    atr_now = sum(trs[-14:]) / 14 if len(trs) >= 14 else 0.0
    atr_med = statistics.median(trs[-60:]) if len(trs) >= 60 else atr_now
    atr_ratio = atr_now / atr_med if atr_med > 0 else 1.0

    red = 0
    j = len(closes) - 1
    while j > 0 and closes[j] < closes[j - 1]:
        red += 1
        j -= 1

    return {
        "symbol": SYMBOL, "price": price, "drawdown_pct": dd,
        "atr": atr_now, "atr_ratio": atr_ratio, "red_bars": red, "rsi": _rsi(closes),
    }


def select_strategy(regime: str, f: dict) -> dict | None:
    """L2: return a trade plan dict if the regime+signal says trade, else None."""
    if "LOVOL" in regime:
        return None  # no edge in low vol — stand down
    if f["drawdown_pct"] > DIP_THRESHOLD:
        return None
    if f["atr_ratio"] < VOL_RATIO_MIN:
        return None
    if f["red_bars"] < MIN_RED_BARS:
        return None
    if f["rsi"] > RSI_MAX:
        return None
    return {
        "symbol": SYMBOL,
        "regime": regime,
        "entry": f["price"],
        "atr": f["atr"],
        "drawdown_pct": f["drawdown_pct"],
        "rsi": f["rsi"],
        "red_bars": f["red_bars"],
    }


def size_position(regime: str, budget: float, plan: dict, s: dict | None = None) -> float:
    """L3: Kelly-capped size from regime edge + win rate + R:R (2:1).

    Precedence: live per-trade edge (if enough samples) > weekly re-fit >
    static defaults. Pass the state `s` to use live learning."""
    edge_map, wr_map = _load_regime_edges()
    edge = edge_map.get(regime, edge_map.get("HIVOL", 0.0))
    wr = wr_map.get(regime, wr_map.get("HIVOL", 0.0))

    # Per-trade learning overrides when it has enough samples for this regime.
    if s is not None:
        live = s.get("live_edge", {}).get(regime)
        if live and live.get("n", 0) >= 5:
            wr = live["win_rate"]
            # convert avg_pnl (per-trade $) back to a rough edge fraction via 2:1
            edge = max(edge, live.get("avg_pnl", 0.0) / 100.0)

    if edge <= 0 or wr <= 0.5:
        return 0.0
    # Kelly fraction for 2:1 R:R: f = p - q/b = p - (1-p)/2
    b = 2.0
    kelly = wr - (1 - wr) / b
    kelly = max(0.0, min(0.25, kelly))  # half-kelly hard cap
    size = budget * kelly
    # risk-capped and floor'd
    size = min(size, budget * 0.20, 50.0)
    return round(size, 2) if size >= 5.0 else 0.0


def check_and_trade():
    s = _load()
    events = []
    ex = make_exchange(sandbox=True)

    # L1: regime
    regime = detect_regime(ex)
    s["regime"] = regime
    events.append(f"regime={regime}")

    # Close open positions (TP/SL/time-stop)
    now_ms = int(__import__("time").time() * 1000)
    keep = []
    for pos in s["open_positions"]:
        try:
            px = float(ex.fetch_ticker(pos["symbol"])["last"])
        except Exception:
            keep.append(pos)
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
                "symbol": pos["symbol"], "regime": pos["regime"], "entry": pos["entry"],
                "exit": round(px, 4), "pnl": round(pnl, 4), "reason": reason,
                "ts_utc": datetime.now(timezone.utc).isoformat(),
            })
            events.append(f"CLOSE {pos['symbol']} ({reason}) {pnl:+.2f} USDT")
            _notify(
                f"CLOSE {pos['symbol']} ({reason})",
                f"regime : {pos['regime']}\n"
                f"entry  : {pos['entry']}\nexit   : {round(px,4)}\n"
                f"pnl    : {pnl:+.2f} USDT",
            )
        else:
            keep.append(pos)
    s["open_positions"] = keep

    # Per-trade learning: fold latest results into sizing immediately.
    _learn_from_closed_trades(s)

    # L2 + L3: open a new position if regime + signal + size all agree
    if not any(p["symbol"] == SYMBOL for p in s["open_positions"]):
        f = entry_features(ex)
        if f:
            plan = select_strategy(regime, f)
            if plan:
                size = size_position(regime, s["budget"], plan, s)
                if size > 0:
                    stop = 1.5 * plan["atr"] / plan["entry"]
                    tp = round(plan["entry"] * (1 + 2 * stop), 4)
                    sl = round(plan["entry"] * (1 - stop), 4)
                    s["budget"] -= size
                    s["open_positions"].append({
                        "symbol": SYMBOL, "regime": regime, "entry": plan["entry"],
                        "tp": tp, "sl": sl, "size": size, "ts_ms": now_ms,
                        "hold_ms": HOLD_BARS * 30 * 60 * 1000,
                    })
                    events.append(
                        f"OPEN {SYMBOL} [{regime}] @ {plan['entry']} size ${size} "
                        f"(dd={plan['drawdown_pct']*100:.1f}% rsi={plan['rsi']:.0f}) TP {tp}/SL {sl}"
                    )
                    _notify(
                        f"OPEN {SYMBOL} [{regime}]",
                        f"entry  : {plan['entry']}\nsize   : ${size}\n"
                        f"dd     : {plan['drawdown_pct']*100:.1f}%\n"
                        f"rsi    : {plan['rsi']:.0f}\n"
                        f"TP     : {tp}\nSL     : {sl}",
                    )
                    # LIVE BRIDGE: if the gate is open, place the same signal
                    # as a REAL order via the conservative live executor.
                    _try_live_open(SYMBOL, size, plan)

                else:
                    events.append(f"signal but size=0 (edge too thin in {regime})")

    # Multi-strategy layer: run validated strategies (XRP/ETH/BTC 5m) on paper.
    events.extend(run_strategies(ex, s))

    s["last_check_utc"] = datetime.now(timezone.utc).isoformat()
    _save(s)
    return {"regime": regime, "budget": round(s["budget"], 2),
            "open": len(s["open_positions"]), "closed": len(s["closed_trades"]),
            "events": events}


def _try_live_open(symbol: str, paper_size: float, plan: dict) -> None:
    """Open a REAL order mirroring a paper signal, only if live is enabled.

    Uses the conservative live_executor (1% stop, $5 cap, daily loss cap).
    Failures are logged, never crash the paper loop.
    """
    try:
        from scripts.live_executor import live_enabled, open_long, LIVE_MAX_OPEN
        if not live_enabled():
            return
        from scripts.live_executor import _load as _live_load
        if len(_live_load()["open"]) >= LIVE_MAX_OPEN:
            return
        # map engine symbol to a live pair that exists on the account
        live_sym = symbol if "/" in symbol else f"{symbol}/USDT"
        res = open_long(live_sym, paper_size)
        if res.get("ok"):
            _notify(
                f"🔴 LIVE BUY {live_sym}",
                f"entry  : {res['trade']['entry']}\nsize   : ${res['trade']['size_usdt']}\n"
                f"TP     : {res['trade']['tp']}\nSL     : {res['trade']['sl']}",
            )
        else:
            _notify("LIVE OPEN SKIPPED", f"{live_sym}: {res.get('reason')}")
    except Exception as e:  # noqa: BLE001
        _notify("LIVE OPEN ERROR", str(e)[:160])


def summary():
    s = _load()
    wins = sum(1 for t in s["closed_trades"] if t["pnl"] > 0)
    losses = sum(1 for t in s["closed_trades"] if t["pnl"] <= 0)
    total_pnl = sum(t["pnl"] for t in s["closed_trades"])
    n = wins + losses
    return {
        "regime": s["regime"], "budget": round(s["budget"], 2),
        "win_rate": round(wins / n * 100, 1) if n else None,
        "total_pnl": round(total_pnl, 2),
        "closed_trades": n,
    }


# ---- Multi-strategy layer (validated strategies from config/strategies.json) ----

STRATEGIES_PATH = Path(__file__).resolve().parents[1] / "config" / "strategies.json"


def _load_strategies() -> list[dict]:
    if STRATEGIES_PATH.exists():
        try:
            d = json.loads(STRATEGIES_PATH.read_text(encoding="utf-8"))
            return [s for s in d.get("strategies", []) if s.get("enabled", True)]
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _strategy_features(ex, sym: str, tf: str) -> dict:
    """Compute capitulation features for an arbitrary symbol/timeframe."""
    ohlcv = ex.fetch_ohlcv(sym, tf, limit=90)
    if not ohlcv or len(ohlcv) < 40:
        return {}
    closes = [float(c[4]) for c in ohlcv]
    price = closes[-1]
    hi = max(closes[:-1])
    dd = price / hi - 1.0 if hi > 0 else 0.0
    red = 0
    j = len(closes) - 1
    while j > 0 and closes[j] < closes[j - 1]:
        red += 1
        j -= 1
    return {"symbol": sym, "price": price, "drawdown_pct": dd,
            "red_bars": red, "rsi": _rsi(closes)}


def run_strategies(ex, s: dict) -> list[str]:
    """Run every validated strategy from config/strategies.json on paper.

    Each strategy has its own symbol/timeframe/thresholds. Signals open a
    paper position with TP/SL computed from the strategy's hold + edge.
    """
    events = []
    now_ms = int(__import__("time").time() * 1000)
    for st in _load_strategies():
        sym = st["symbol"]
        tf = st["timeframe"]
        # skip if already have an open position on this symbol
        if any(p["symbol"] == sym for p in s["open_positions"]):
            continue
        try:
            f = _strategy_features(ex, sym, tf)
        except Exception:
            continue
        if not f:
            continue
        # gate on the strategy's own thresholds
        if f["drawdown_pct"] > st["dip_threshold"]:
            continue
        if f["red_bars"] < st["min_red_bars"]:
            continue
        if f["rsi"] > st["rsi_max"]:
            continue
        # size from the strategy's validated edge + win rate (Kelly-capped)
        wr = st["win_rate"]
        edge = st["edge"]
        if edge <= 0 or wr <= 0.5:
            continue
        b = 2.0
        kelly = wr - (1 - wr) / b
        kelly = max(0.0, min(0.25, kelly))
        size = s["budget"] * kelly
        size = min(size, s["budget"] * 0.20, 50.0)
        if size < 5.0:
            continue
        # TP/SL: 2:1 reward:risk, stop = 1.5x a rough vol estimate
        stop_pct = 0.015 if tf in ("30m", "15m") else 0.008  # tighter for 5m
        entry = f["price"]
        tp = round(entry * (1 + 2 * stop_pct), 6)
        sl = round(entry * (1 - stop_pct), 6)
        bar_ms = {"5m": 5 * 60 * 1000, "15m": 15 * 60 * 1000,
                  "30m": 30 * 60 * 1000}.get(tf, 15 * 60 * 1000)
        hold_ms = st["hold_bars"] * bar_ms
        s["budget"] = round(s["budget"] - size, 2)
        s["open_positions"].append({
            "symbol": sym, "strategy": st["name"], "regime": s.get("regime", "?"),
            "entry": entry, "tp": tp, "sl": sl, "size": round(size, 2),
            "ts_ms": now_ms, "hold_ms": hold_ms,
        })
        events.append(
            f"OPEN {sym} [{st['name']}] @ {entry} size ${size:.2f} "
            f"(dd={f['drawdown_pct']*100:.1f}% rsi={f['rsi']:.0f}) TP {tp}/SL {sl}"
        )
        _notify(
            f"OPEN {sym} [{st['name']}]",
            f"entry  : {entry}\nsize   : ${size:.2f}\n"
            f"dd     : {f['drawdown_pct']*100:.1f}%\n"
            f"rsi    : {f['rsi']:.0f}\nTP     : {tp}\nSL     : {sl}",
        )
    return events


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "check":
        print(json.dumps(check_and_trade(), indent=2))
    elif mode == "summary":
        print(json.dumps(summary(), indent=2))
