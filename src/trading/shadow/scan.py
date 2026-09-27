"""Live scan (P4c): each minute, what every shadow book's rule would say if the day closed at the CURRENT price.

PROVISIONAL by construction: it is never a signal, never enters the hash-chained record, and reads each strategy's
state strictly read-only (copies; test-enforced), so a preview can never change what the real decision will be.
Output: <shadow>/scan/<lane>__<strategy>.json, overwritten each minute (latest only), served by the feed's /v1/scan.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from trading.replay.bars import INTERVAL_MS

D = INTERVAL_MS["1d"]
W = 7 * D
MONDAY = 4 * D
LABEL = "PROVISIONAL: what the rule would do if the day closed at the current price. Not a signal, not recorded."


def _price_now(ctx, sym: str):
    h = ctx.history(sym, "1m", 1)
    return (h[-1].close, h[-1].end_ms) if h else (None, None)


def _closes(ctx, sym: str, n: int) -> list[float]:
    return [b.close for b in ctx.history(sym, "1d", n)]


def next_daily(now_ms: int) -> int:
    return now_ms - now_ms % D + D


def next_weekly(now_ms: int) -> int:
    k = (now_ms - MONDAY) // W + 1
    return MONDAY + k * W


def scan_donchian(s, ctx) -> list[dict]:
    out = []
    for sym in sorted(s._seen):
        p, _ = _price_now(ctx, sym)
        c = _closes(ctx, sym, max(s._lb) + 1)
        if p is None or len(c) < 2:
            continue
        arr = c + [p]
        opened, nearest = 0, None
        for n in s._lb:
            if len(arr) < n + 1:
                continue
            prior = arr[-n - 1:-1]
            hi, lo = max(prior), min(prior)
            stop = s._stop.get((sym, n))                       # read only: never written here
            if stop is not None:
                opened += p >= max(stop, (hi + lo) / 2)
            elif p > hi:
                opened += 1
            else:
                d = hi / p - 1
                nearest = d if nearest is None else min(nearest, d)
        out.append({"symbol": sym, "price": p, "score": opened / len(s._lb),
                    "state": f"{opened}/{len(s._lb)} lookbacks open",
                    "detail": "all open" if nearest is None and opened else
                    (f"next lookback opens at +{nearest * 100:.2f}%" if nearest is not None else "none open")})
    return out


def scan_tsmom(s, ctx) -> list[dict]:
    out = []
    stressed = s._index < (1 - s._dd) * s._peak
    for sym in s._assets:
        p, _ = _price_now(ctx, sym)
        c = _closes(ctx, sym, s._n + 1)
        if p is None or len(c) < s._n + 1:
            continue
        r = p / c[1] - 1                                       # the 30-day window that today's close would end
        out.append({"symbol": sym, "price": p, "score": r, "state": "would HOLD" if r > 0 else "would be CASH",
                    "detail": f"30d return {r * 100:+.2f}%" + ("; market >15% off peak: exposure x0.5" if stressed else "")})
    return out


def scan_xs(s, ctx) -> list[dict]:
    scores = {}
    for sym in sorted(s._seen):
        p, _ = _price_now(ctx, sym)
        c = _closes(ctx, sym, s._days + 1)
        if p is None or len(c) < s._days + 1 or c[1] <= 0:
            continue
        scores[sym] = (p / c[1] - 1, p)
    k = max(1, len(scores) // s._q) if scores else 0
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1][0], kv[0]))
    return [{"symbol": sym, "price": p, "score": r, "state": "TOP quintile" if i < k else f"rank {i + 1}/{len(ranked)}",
             "detail": f"3-week return {r * 100:+.2f}%"} for i, (sym, (r, p)) in enumerate(ranked)]


def scan_ctrend(s, ctx) -> list[dict]:
    sc = dict(s.last_ctrend)                                    # copy of the last weekly scores
    k = max(1, len(sc) // s._q) if sc else 0
    ranked = sorted(sc.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"symbol": sym, "price": _price_now(ctx, sym)[0], "score": v,
             "state": "TOP quintile" if i < k else f"rank {i + 1}/{len(ranked)}",
             "detail": "last weekly CTREND score (recomputed only at the Monday close)"} for i, (sym, v) in enumerate(ranked)]


SCANNERS = {"donchian_ensemble": (scan_donchian, next_daily), "tsmom_risk_managed": (scan_tsmom, next_daily),
            "xs_momentum": (scan_xs, next_weekly), "ctrend_lite": (scan_ctrend, next_weekly)}


def scan_book(book, now_ms: int) -> dict | None:
    fn = SCANNERS.get(book.name)
    if fn is None or book.stopped:
        return None
    strat, ctx = book.scanner.core.strategies[0], book.scanner.core.ctx
    coins = fn[0](strat, ctx)
    return {"v": 1, "simulated": True, "provisional": True, "label": LABEL, "lane": book.lane, "strategy": book.name,
            "as_of_ms": now_ms, "next_decision_ms": fn[1](now_ms), "coins": coins}


def write_scans(fleet, root: Path | str, now_ms: int) -> int:
    d = Path(root) / "scan"
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    for b in fleet.books:
        try:
            doc = scan_book(b, now_ms)
        except Exception as e:  # noqa: BLE001 - a preview must never disturb the fleet
            doc = {"v": 1, "simulated": True, "provisional": True, "lane": b.lane, "strategy": b.name,
                   "as_of_ms": now_ms, "error": f"{type(e).__name__}: {str(e)[:120]}", "coins": []}
        if doc is None:
            continue
        tmp = d / f".{b.lane}__{b.name}.json.tmp"
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, d / f"{b.lane}__{b.name}.json")
        n += 1
    return n
