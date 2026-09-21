#!/usr/bin/env python3
"""Price-action pattern classifier + explainable 0-100 score.

Ported from GMGN's `gmgn-kline-pattern` SKILL.md (MIT-licensed okx/gmgn-skills
family), 2026-09-18. Their spec is computed entirely from the kline response —
no API, no CLI, no key — so the *methodology* transfers to our CEX candles even
though their marketplace tooling does not.

What was taken and why: their six measurements and the scored rule table are a
disciplined, fully-explainable read of a chart, and the discipline matches ours —
every point states its reason, a zero denominator marks a measurement UNAVAILABLE
and skips its rules rather than silently scoring 0, and too little data means
refuse rather than invent.

What was NOT taken: any claim of edge. Their own footer says it best —
"A pattern describes what already happened. It does not predict price."
Adopting this is only justified if it changes a measured number; see
scripts/pattern_backtest.py.

Their note on provenance: thresholds are a hand-computable simplification of an
EMA9/EMA21 + least-squares-slope + ATR reference, checked on 7 real candle sets
where trend direction agreed 6/7 and the final label 6/7.
"""
from __future__ import annotations

import datetime as dt

PATTERNS = [
    "vertical run-up", "uptrend channel", "breakdown", "bounce off the lows",
    "distribution at highs", "slow bleed", "basing at lows", "wide chop",
    "bullish consolidation", "bearish consolidation", "sideways consolidation",
]


def _mean(v):
    return sum(v) / len(v) if v else None


def measure(highs, lows, closes, volumes):
    """The six measurements. None means UNAVAILABLE (zero denominator)."""
    n = len(closes)
    m = {}

    # 1. trend_up — three-valued. Equality is FLAT, not "down"; a perfectly flat
    #    chart is not a bearish one and must not be penalised for nothing.
    a9, a21 = _mean(closes[-9:]), _mean(closes[-21:])
    if a9 is None or a21 is None:
        m["trend_up"] = None
    elif a9 > a21:
        m["trend_up"] = "up"
    elif a9 < a21:
        m["trend_up"] = "down"
    else:
        m["trend_up"] = "flat"

    # 2. slope — both ends averaged so one spike on the last bar cannot dominate.
    if n >= 24:
        old = _mean(closes[-24:-19])
        new = _mean(closes[-5:])
        m["slope"] = None if not old else (new - old) / old
    else:
        m["slope"] = None if not closes[0] else (closes[-1] - closes[0]) / closes[0]

    # 3. volatility — mean of (high-low)/close over the last 14
    seg = list(zip(highs[-14:], lows[-14:], closes[-14:]))
    vals = [(h - l) / c for h, l, c in seg if c]
    m["volatility"] = _mean(vals) if vals else None

    # 4. drawdown from the range high
    hi = max(highs) if highs else 0
    m["drawdown"] = None if not hi else (hi - closes[-1]) / hi

    # 5. vol_ratio — last bar vs the 20 before it
    base = _mean(volumes[-21:-1])
    m["vol_ratio"] = None if not base else volumes[-1] / base

    # 6. up_from_low
    lo = min(lows) if lows else 0
    m["up_from_low"] = None if not lo else (closes[-1] - lo) / lo

    return m


def classify(m):
    """First match wins, evaluated in the spec's order."""
    s, dd, ufl = m.get("slope"), m.get("drawdown"), m.get("up_from_low")
    vol, trend = m.get("volatility"), m.get("trend_up")

    if s is not None and dd is not None:
        if s > 0.25 and dd < 0.12:
            return "vertical run-up"
        if s > 0.08 and dd < 0.25:
            return "uptrend channel"
        if dd > 0.55 and s < -0.10:
            return "breakdown"
        # Without this, a token down 60-90% with a short pop reads as
        # "consolidation", which badly understates where it actually is.
        if dd > 0.55 and s > 0.02:
            return "bounce off the lows"
        if dd > 0.35 and abs(s) < 0.08:
            return "distribution at highs"
    if s is not None and s < -0.20:
        return "slow bleed"
    if (s is not None and abs(s) < 0.05 and vol is not None and vol < 0.05
            and ufl is not None and ufl < 0.20):
        return "basing at lows"
    if s is not None and abs(s) < 0.08 and vol is not None and vol > 0.08:
        return "wide chop"
    if trend == "up":
        return "bullish consolidation"
    if trend == "down":
        return "bearish consolidation"
    return "sideways consolidation"


def score_pattern(highs, lows, closes, volumes):
    """Returns (pattern, score, m, evidence, not_measured). Never raises on
    short input — returns None instead, rather than inventing a pattern."""
    n = len(closes)
    if n < 8:
        return None

    m = measure(highs, lows, closes, volumes)
    pattern = classify(m)
    ev, skipped, pts = [], [], 50
    s, dd = m.get("slope"), m.get("drawdown")
    vr, trend = m.get("vol_ratio"), m.get("trend_up")
    last_green = closes[-1] >= (closes[-2] if n >= 2 else closes[-1])

    # --- trend_up
    if trend == "up":
        pts += 12; ev.append(("+12", "9-bar avg above 21-bar avg — short-term bullish structure"))
    elif trend == "down":
        pts -= 12; ev.append(("-12", "9-bar avg below 21-bar avg — short-term bearish structure"))
    else:
        ev.append(("0", "9/21 averages equal — no structure either way"))

    # --- slope
    if s is None:
        skipped.append("slope unavailable (zero denominator) — 4 slope rules skipped")
    elif s > 0.15:
        pts += 15; ev.append(("+15", f"last 20 bars trend up, {s*100:+.1f}%"))
    elif s > 0.02:
        pts += 6; ev.append(("+6", f"mild uptrend, {s*100:+.1f}%"))
    elif s < -0.15:
        pts -= 15; ev.append(("-15", f"last 20 bars trend down, {s*100:+.1f}%"))
    elif s >= -0.15 and s < -0.02:
        pts -= 6; ev.append(("-6", f"slow bleed, {s*100:+.1f}%"))
    else:
        ev.append(("0", "sideways — no direction yet"))

    # --- volume ratio
    if vr is None:
        skipped.append("vol_ratio unavailable (zero average volume) — 3 rules skipped")
    elif vr > 3:
        if last_green:
            pts += 8; ev.append(("+8", f"latest bar {vr:.1f}x volume — volume-backed push up"))
        else:
            pts -= 10; ev.append(("-10", f"latest bar {vr:.1f}x volume — volume-backed dump"))
    elif vr < 0.3:
        pts -= 5; ev.append(("-5", f"volume shrank to {vr*100:.0f}% of average — attention fading"))

    # --- drawdown
    if dd is None:
        skipped.append("drawdown unavailable (zero range high) — 3 rules skipped")
    elif dd > 0.60:
        pts -= 15; ev.append(("-15", f"down {dd*100:.0f}% from range high — catching-a-knife risk"))
    elif dd > 0.30:
        pts -= 6; ev.append(("-6", f"down {dd*100:.0f}% from range high"))
    elif dd < 0.05:
        pts += 8; ev.append(("+8", "trading right at the range high"))

    # --- divergence: new price high on shrinking volume
    if n > 40:
        seg = closes[-40:]
        hi20, hi40 = max(seg[-20:]), max(seg[:20])
        v20, v40 = _mean(volumes[-20:]), _mean(volumes[-40:-20])
        if hi20 > hi40 * 1.02 and v20 is not None and v40 and v20 < v40 * 0.7:
            pts -= 10
            ev.append(("-10", "new price high on shrinking volume — move lacks participation"))

    pts = max(0, min(100, pts))

    # context only, never scored
    if m.get("volatility") is not None:
        if m["volatility"] > 0.15:
            ev.append(("ctx", f"very high volatility ({m['volatility']*100:.1f}%) — size down"))
        elif m["volatility"] < 0.02:
            ev.append(("ctx", f"low volatility ({m['volatility']*100:.1f}%)"))
    run = 1
    for i in range(n - 2, -1, -1):
        same = (closes[i] <= closes[i + 1]) if last_green else (closes[i] >= closes[i + 1])
        if same:
            run += 1
        else:
            break
    if run >= 5:
        ev.append(("ctx", f"{run} consecutive {'green' if last_green else 'red'} candles "
                           + ("— short-term overbought" if last_green
                              else "— downtrend not exhausted")))

    return {"pattern": pattern, "score": pts, "m": m,
            "evidence": ev, "not_measured": skipped, "bars": n}


def drop_incomplete(ohlcv, period_s=86400, now=None):
    """Trim the still-forming final candle.

    🔴 PITFALL FOUND 2026-09-19 while porting this to DAILY candles:
    Binance's kline endpoint includes the in-progress bar. At 00:43 UTC only
    ~3% of the current day had elapsed, so `vol_ratio` read 0.026x / 0.041x /
    0.037x for BTC / ARB / SOL respectively — an almost exact match to the
    elapsed fraction. It was measuring *how much of today has passed*, not
    volume behaviour.

    Consequence if unfixed: the `vol_ratio < 0.3 -> -5` rule fires on EVERY
    symbol unconditionally (so every score is 5 too low), and the two
    volume-backing rules at `> 3x` become unreachable dead code. The same
    artifact would corrupt any volume-ratio read on any timeframe.

    Excluding the in-progress bar gave 1.755x / 1.766x / 2.190x — real readings.
    """
    if not ohlcv:
        return ohlcv
    if now is None:
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
    last_open = dt.datetime.fromtimestamp(ohlcv[-1][0] / 1000, dt.timezone.utc)
    if (now - last_open).total_seconds() < period_s:
        return ohlcv[:-1]
    return ohlcv


def score_ohlcv(ohlcv, drop_last=True, period_s=86400):
    """Preferred entry point: takes raw ccxt OHLCV. Drops the in-progress bar
    by default — see drop_incomplete() for why that is not optional."""
    o = drop_incomplete(ohlcv, period_s=period_s) if drop_last else ohlcv
    if len(o) < 8:
        return None
    return score_pattern([c[2] for c in o], [c[3] for c in o],
                         [c[4] for c in o], [c[5] for c in o])


def describe(res, label=""):
    if not res:
        return f"{label}: not enough candles to read a pattern"
    m = res["m"]
    out = [f"{label}  Pattern: {res['pattern']}   Score: {res['score']}/100"]
    for k in ("slope", "volatility", "vol_ratio", "drawdown", "up_from_low"):
        v = m.get(k)
        if v is None:
            out.append(f"    {k:<12} n/a")
        elif k == "vol_ratio":
            out.append(f"    {k:<12} {v:.2f}x")
        else:
            out.append(f"    {k:<12} {v*100:+.1f}%")
    out.append(f"    trend_up     {m.get('trend_up')}")
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, "/opt/trading-agent/scripts")
    sys.path.insert(0, "/opt/trading-agent/src")
    from trading.exchange.live_client import make_live_exchange
    ex = make_live_exchange()
    for sym in ["BTC/USDT", "ARB/USDT", "NEAR/USDT", "DOGE/USDT", "SOL/USDT"]:
        o = ex.fetch_ohlcv(sym, "1d", limit=120)
        r = score_ohlcv(o)
        print(describe(r, f"{sym:<11}"))
        if r:
            for d, why in r["evidence"]:
                print(f"      {d:>4}  {why}")
            for nm in r["not_measured"]:
                print(f"      n/a   {nm}")
        print()
