"""Window statistics, BTC regime, and simple risk numbers for REPLAY runs (numpy only)."""
from __future__ import annotations

import math

import numpy as np

from trading.replay.bars import INTERVAL_MS, Bar

DAY = INTERVAL_MS["1d"]


def window_stats(ts, curve, start_equity: float, t0: int, t1: int) -> dict:
    """Return and max drawdown of an equity curve sliced to (t0, t1] (a slice of one continuous run).

    Equity is marked to mid at every 1m close; the start value is the equity at t0 (or the account's
    starting equity if t0 precedes the curve).
    """
    ts = np.frombuffer(ts, dtype=np.int64) if not isinstance(ts, np.ndarray) else ts
    eq = np.frombuffer(curve, dtype=np.float64) if not isinstance(curve, np.ndarray) else curve
    i0 = int(np.searchsorted(ts, t0, side="right"))
    i1 = int(np.searchsorted(ts, t1, side="right"))
    if i1 <= i0:
        return {"ret": None, "max_dd": None, "n": 0}
    e0 = eq[i0 - 1] if i0 > 0 else start_equity
    seg = eq[i0:i1]
    peak = np.maximum.accumulate(np.concatenate(([e0], seg)))[1:]
    return {"ret": float(seg[-1] / e0 - 1), "max_dd": float(np.max(1 - seg / peak)), "n": int(i1 - i0)}


def daily_sharpe(ts, curve, t0: int | None = None, t1: int | None = None) -> float | None:
    """Annualised (sqrt 365) Sharpe of daily equity returns (last mark of each UTC day). Informational only;
    the gate (T3.3) uses the Deflated Sharpe over all logged trials. With t0/t1, only marks in (t0, t1]
    count, plus the last mark at or before t0 as the starting value."""
    ts = np.frombuffer(ts, dtype=np.int64) if not isinstance(ts, np.ndarray) else ts
    eq = np.frombuffer(curve, dtype=np.float64) if not isinstance(curve, np.ndarray) else curve
    if t0 is not None or t1 is not None:
        i0 = max(int(np.searchsorted(ts, t0, side="right")) - 1, 0) if t0 is not None else 0
        i1 = int(np.searchsorted(ts, t1, side="right")) if t1 is not None else len(ts)
        ts, eq = ts[i0:i1], eq[i0:i1]
    day = ts // DAY
    last_of_day = np.nonzero(np.append(day[1:] != day[:-1], True))[0]
    d = eq[last_of_day]
    if len(d) < 3:
        return None
    r = d[1:] / d[:-1] - 1
    sd = float(np.std(r, ddof=1))
    return None if sd == 0 else float(np.mean(r) / sd * math.sqrt(365))


def daily_series(ts, curve) -> list[list]:
    """[[utc_day_start_ms, last equity mark of that day], ...], the sampling daily_sharpe uses (L2a)."""
    ts = np.frombuffer(ts, dtype=np.int64) if not isinstance(ts, np.ndarray) else ts
    eq = np.frombuffer(curve, dtype=np.float64) if not isinstance(curve, np.ndarray) else curve
    if not len(ts):
        return []
    day = ts // DAY
    last_of_day = np.nonzero(np.append(day[1:] != day[:-1], True))[0]
    return [[int(day[i] * DAY), float(eq[i])] for i in last_of_day]


def regime(btc_daily: tuple[Bar, ...], t0: int, t1: int, sma: int = 200) -> dict:
    """BTC regime over (t0, t1]: total return, annualised vol of daily returns, share of days above SMA200.

    Labels use fixed, stated thresholds: bull > +20%, bear < -20%, else range; high-vol if vol > 60%.
    """
    closes = np.array([b.close for b in btc_daily])
    ends = np.array([b.end_ms for b in btc_daily], dtype=np.int64)
    inw = (ends > t0) & (ends <= t1)
    idx = np.nonzero(inw)[0]
    if len(idx) < 2:
        return {"label": "UNKNOWN", "reason": "fewer than 2 BTC daily bars in window"}
    base = closes[idx[0] - 1] if idx[0] > 0 else closes[idx[0]]
    total = float(closes[idx[-1]] / base - 1)
    w = closes[max(idx[0] - 1, 0): idx[-1] + 1]
    r = w[1:] / w[:-1] - 1
    vol = float(np.std(r, ddof=1) * math.sqrt(365))
    above = [closes[i] > closes[i - sma + 1: i + 1].mean() for i in idx if i >= sma - 1]
    trend = "bull" if total > 0.20 else "bear" if total < -0.20 else "range"
    return {"label": f"{trend}, {'high' if vol > 0.60 else 'low'}-vol", "btc_return": total,
            "btc_ann_vol": vol, "share_days_above_sma200": (float(np.mean(above)) if above else None),
            "days_with_sma200": len(above), "days": int(len(idx)),
            "thresholds": "bull>+20%, bear<-20%, else range; high-vol if annualised vol>60%"}


def trade_stats(trades: list[dict]) -> dict:
    if not trades:
        return {"closed": 0, "win_rate_net": None, "avg_win_net": None, "avg_loss_net": None}
    net = [t["net_ret"] for t in trades]
    wins, losses = [x for x in net if x > 0], [x for x in net if x <= 0]
    return {"closed": len(trades), "win_rate_net": len(wins) / len(net),
            "avg_win_net": (sum(wins) / len(wins)) if wins else None,
            "avg_loss_net": (sum(losses) / len(losses)) if losses else None,
            "fees_paid": sum(t["fee"] for t in trades)}
