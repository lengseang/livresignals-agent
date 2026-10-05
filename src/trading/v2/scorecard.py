"""The scorecard (docs/loop/designs/V2-16 Part A): grade every logged decision against what the market did next.
"Accuracy" was unmeasured: Jev's and the gateway's probabilities were logged and never scored. This scores them.

Per decision record (cycle chain, `v2_decision_cycle`) with a snapshot, from CLOSED 1h bars after the decision:
  fwd_1h / fwd_4h / fwd_24h   close of the first 1h bar ending at or after t + h, vs the decision price
  barrier                     did a long hit the approved +7% before -3.5% within 14 days (path from the first 1h bar
                              that OPENS at or after t: up to 59 minutes of path are not seen; stop first in a bar)
A record whose horizon has not elapsed is PENDING, never guessed.

Per source (jev | gateway (Jev down) | gateway_dual (computed beside Jev)):
  action mix; directional hit rate (sign of fwd_24h for LONG/SHORT calls); mean fwd_24h of LONG calls vs all;
  Brier + AUC of P(LONG) and of expected_edge against the barrier label, with the base rate's Brier for reference;
  a calibration table. Plus the Jev x gateway agreement matrix.
Position advice: for each advised EXIT/REDUCE/HOLD, was exiting at the decision price better than holding the bracket
(stop/target as logged) for up to 14 days? Precision of exits, and of holds.

Pure scoring (`score`) is separated from I/O (`main`), and deterministic for the same inputs.
"""
from __future__ import annotations

import argparse
import json
import math
from bisect import bisect_left
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from trading.replay.bars import Bar

H = 3_600_000
HORIZONS = {"fwd_1h": H, "fwd_4h": 4 * H, "fwd_24h": 24 * H}
STOP, TARGET, BARRIER_MS = 0.035, 0.07, 14 * 24 * H
INACTIVE = ("HOLD", "NO_TRADE")
E13_MIN_EXITS = 20


def _norm(a: str) -> str:
    return "INACTIVE" if a in INACTIVE else a


class Path1h:
    """Closed 1h bars of one symbol, indexed by end time."""

    def __init__(self, bars: Sequence[Bar]):
        self.bars = sorted((b for b in bars if b.interval == "1h"), key=lambda b: b.end_ms)
        self.ends = [b.end_ms for b in self.bars]
        self.opens = [b.open_ms for b in self.bars]

    def close_at_or_after(self, t: int) -> Optional[float]:
        i = bisect_left(self.ends, t)
        return self.bars[i].close if i < len(self.bars) else None

    def barrier(self, t: int, price: float, stop: float = STOP, target: float = TARGET,
                horizon: int = BARRIER_MS) -> Optional[float]:
        """1.0 target first, 0.0 stop first / both in a bar / timeout; None if the horizon has not elapsed."""
        if not self.ends or self.ends[-1] < t + horizon:
            return None
        i = bisect_left(self.opens, t)
        lo, hi = price * (1 - stop), price * (1 + target)
        while i < len(self.bars) and self.bars[i].open_ms < t + horizon:
            b = self.bars[i]
            if b.low <= lo:
                return 0.0
            if b.high >= hi:
                return 1.0
            i += 1
        return 0.0

    def hold_outcome(self, t: int, side: str, stop: Optional[float], target: Optional[float],
                     horizon: int = BARRIER_MS) -> Optional[float]:
        """Exit price of holding the bracket from t (None if not yet known)."""
        if not self.ends or self.ends[-1] < t + horizon:
            return None
        i = bisect_left(self.opens, t)
        last = None
        while i < len(self.bars) and self.bars[i].open_ms < t + horizon:
            b = self.bars[i]
            if side == "LONG":
                if stop is not None and b.low <= stop:
                    return min(stop, b.open)
                if target is not None and b.high >= target:
                    return max(target, b.open)
            else:
                if stop is not None and b.high >= stop:
                    return max(stop, b.open)
                if target is not None and b.low <= target:
                    return min(target, b.open)
            last = b.close
            i += 1
        return last


def _brier(ps, ys) -> Optional[float]:
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps) if ps else None


def _auc(ps, ys) -> Optional[float]:
    pos = [p for p, y in zip(ps, ys) if y == 1.0]
    neg = [p for p, y in zip(ps, ys) if y == 0.0]
    if not pos or not neg:
        return None
    wins = sum((1.0 if a > b else 0.5 if a == b else 0.0) for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def _calibration(ps, ys, edges=(0.0, 0.2, 0.4, 0.5, 0.6, 0.8, 1.0001)) -> list[dict]:
    out = []
    for lo, hi in zip(edges, edges[1:]):
        sel = [(p, y) for p, y in zip(ps, ys) if lo <= p < hi]
        if sel:
            out.append({"bin": [lo, min(hi, 1.0)], "n": len(sel), "mean_p": sum(p for p, _ in sel) / len(sel),
                        "hit_rate": sum(y for _, y in sel) / len(sel)})
    return out


def _grade_decision(dec: dict, t: int, price: float, path: Path1h) -> dict:
    g = {"action": dec["action"], "confidence": dec["confidence"]}
    for k, h in HORIZONS.items():
        c = path.close_at_or_after(t + h) if path.ends and path.ends[-1] >= t + h else None
        g[k] = (c / price - 1.0) if c is not None else None
    g["barrier"] = path.barrier(t, price)
    probs = dec.get("probabilities") or {}
    g["p_long"] = probs.get("LONG")
    g["edge"] = (dec.get("signals") or {}).get("expected_edge")
    return g


def _summarise(gs: list[dict]) -> dict:
    graded = [g for g in gs if g["fwd_24h"] is not None]
    out: dict = {"decisions": len(gs), "graded_24h": len(graded),
                 "action_mix": dict(Counter(g["action"] for g in gs))}
    dirs = [g for g in graded if g["action"] in ("LONG", "SHORT")]
    if dirs:
        hits = [(g["fwd_24h"] > 0) == (g["action"] == "LONG") for g in dirs]
        out["directional_hit_rate_24h"] = sum(hits) / len(hits)
        out["directional_n"] = len(dirs)
    longs = [g["fwd_24h"] for g in graded if g["action"] == "LONG"]
    if graded:
        out["mean_fwd_24h_all"] = sum(g["fwd_24h"] for g in graded) / len(graded)
    if longs:
        out["mean_fwd_24h_long_calls"] = sum(longs) / len(longs)
    lab = [g for g in gs if g["barrier"] is not None]
    out["graded_barrier"] = len(lab)
    if lab:
        ys = [g["barrier"] for g in lab]
        base = sum(ys) / len(ys)
        out["barrier_base_rate"] = base
        out["brier_base_rate"] = _brier([base] * len(ys), ys)
        for key in ("p_long", "edge"):
            sel = [(g[key], g["barrier"]) for g in lab if g[key] is not None]
            if sel:
                ps, yy = [p for p, _ in sel], [y for _, y in sel]
                out[key] = {"n": len(sel), "brier": _brier(ps, yy), "auc": _auc(ps, yy),
                            "calibration": _calibration(ps, yy)}
    return out


def score(records: Sequence[dict], paths: Mapping[str, Path1h]) -> dict:
    by_src: dict[str, list] = defaultdict(list)
    agree = Counter()
    advice: dict[str, list] = defaultdict(list)
    pending = 0
    for r in records:
        if r.get("type") != "v2_decision_cycle" or not r.get("state"):
            continue
        sym, t = r["symbol"], int(r["ts_event_ms"])
        path = paths.get(sym)
        if path is None:
            continue
        price = r["state"]["market"]["price"]
        if r.get("advice"):
            a = r["advice"]
            pos = a["position"]
            out = path.hold_outcome(t, pos["side"], pos.get("stop"), pos.get("target"))
            if out is None:
                pending += 1
                continue
            s = 1 if pos["side"] == "LONG" else -1
            exit_better = s * (price - out) > 0
            for src, act in ((a["source"], a["action"]),
                             ("gateway_dual", (r.get("gateway_dual_run") or {}).get("action"))):
                if act:
                    advice[src].append((act, exit_better, s * (price - out) / price))
            continue
        dec = r.get("decision")
        if not dec:
            continue
        src = "jev" if dec["source"] == "jev" else (
            "gateway" if dec.get("fallback_strategy") == "system_one_gateway" else "strategy_vote")
        g = _grade_decision(dec, t, price, path)
        if g["barrier"] is None:             # its longest horizon (14 days) has not elapsed
            pending += 1
        by_src[src].append(g)
        dual = (r.get("gateway_dual_run") or {}).get("decision")
        if dual:
            by_src["gateway_dual"].append(_grade_decision(dual, t, price, path))
            agree[(_norm(dec["action"]), _norm(dual["action"]))] += 1
    adv_out = {}
    for src, rows in advice.items():
        ex = [b for act, b, _ in rows if act in ("EXIT", "REDUCE")]
        hold = [not b for act, b, _ in rows if act in ("HOLD", "TIGHTEN_STOP")]
        adv_out[src] = {"graded": len(rows), "exits": len(ex), "exit_precision": (sum(ex) / len(ex)) if ex else None,
                        "holds": len(hold), "hold_precision": (sum(hold) / len(hold)) if hold else None,
                        "mean_exit_minus_hold": sum(d for _, _, d in rows) / len(rows) if rows else None}
        # owner decision E13: thresholds stay until evidence says otherwise. >= 20 graded exits that beat holding less
        # than half the time -> flag a review (raise EXIT/REDUCE to 0.80). Flag only; nothing changes by itself.
        adv_out[src]["e13_review"] = bool(len(ex) >= E13_MIN_EXITS and sum(ex) / len(ex) < 0.5)
    return {"sources": {k: _summarise(v) for k, v in sorted(by_src.items())},
            "agreement_jev_x_gateway": {f"{a}|{b}": n for (a, b), n in sorted(agree.items())},
            "position_advice": adv_out, "pending": pending}


def read_cycles(out_dir: Path) -> list[dict]:
    from trading.v2.paper import read_chain
    d = Path(out_dir) / "v2cycle" / "pipeline"
    return read_chain(d) if d.exists() else []


def main(argv=None, fetch: Optional[Callable[[str, int, int], Sequence[Bar]]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, type=Path, help="the runner's output directory")
    a = ap.parse_args(argv)
    recs = read_cycles(a.out)
    if not recs:
        print("no cycle records yet")
        return 0
    syms = sorted({r["symbol"] for r in recs})
    t0 = min(int(r["ts_event_ms"]) for r in recs)
    if fetch is None:
        from trading.v2.binance_public import BinancePublic
        feed = BinancePublic()
        feed.sync_clock()
        now = feed.now_ms()
        need = int(math.ceil((now - t0) / H)) + 2
        fetch = lambda s, n, now_=now: feed.history(s, "1h", n, now_ms=now_)   # noqa: E731
    else:
        need = 0
    paths = {s: Path1h(fetch(s, max(need, 24), 0)) for s in syms}
    res = score(recs, paths)
    (a.out / "scorecard.json").write_text(json.dumps(res, indent=1, sort_keys=True))
    for src, s in res["sources"].items():
        pl = s.get("p_long") or {}
        print(f"{src:14s} n={s['decisions']:5d} graded24h={s['graded_24h']:5d} mix={s['action_mix']} "
              f"hit24h={s.get('directional_hit_rate_24h')} brier(pLONG)={pl.get('brier')} vs base {s.get('brier_base_rate')}")
    print("agreement", res["agreement_jev_x_gateway"], "advice", res["position_advice"], "pending", res["pending"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
