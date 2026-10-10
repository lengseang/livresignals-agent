#!/usr/bin/env python3
"""Self-improvement loop — analyze trades, generate ranked suggestions, report.

The "full-time trader mindset" engine. After trades accumulate, this:
  1. Computes live win-rate / expectancy / profit-factor per regime
  2. Detects drift (is the edge holding? which regime is decaying?)
  3. Generates concrete, ranked improvement suggestions
  4. Writes a report + (optionally) pings Telegram

This is the "report → suggest → improve → loop" step. Run it weekly (or on
demand) to feed the next round of tuning.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return default


def analyze() -> dict:
    now = datetime.now(timezone.utc).isoformat()

    # Paper trades (unified track — the ACTIVE learning ledger) + live trades.
    # unified-paper.json holds every paper trade across all edges (validated /
    # loose / pullback) with 'edge' + 'loose' tags; adaptive-state.json is legacy.
    unified = _load_json(DATA / "unified-paper.json", {"open": [], "closed": []})
    live = _load_json(DATA / "live-trades.json", {"closed": []})

    paper = unified.get("closed", [])
    real = live.get("closed", [])

    def _stats(trades):
        if not trades:
            return None
        wins = [t for t in trades if t.get("pnl", 0) > 0]
        losses = [t for t in trades if t.get("pnl", 0) <= 0]
        n = len(trades)
        wr = len(wins) / n if n else 0
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        gp = sum(t.get("pnl", 0) for t in wins)
        gl = abs(sum(t.get("pnl", 0) for t in losses))
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        ev = total_pnl / n if n else 0
        return {"n": n, "win_rate": round(wr * 100, 1), "ev": round(ev, 4),
                "profit_factor": round(pf, 2), "total_pnl": round(total_pnl, 4)}

    paper_stats = _stats(paper)
    live_stats = _stats(real)

    # Per-regime breakdown (paper)
    by_regime = {}
    for t in paper:
        r = by_regime.setdefault(t.get("regime", "?"), {"wins": 0, "n": 0, "pnl": []})
        r["n"] += 1
        if t.get("pnl", 0) > 0:
            r["wins"] += 1
        r["pnl"].append(t.get("pnl", 0))
    regime_rows = []
    for r, d in by_regime.items():
        regime_rows.append({
            "regime": r, "n": d["n"],
            "win_rate": round(d["wins"] / d["n"] * 100, 1) if d["n"] else 0,
            "avg_pnl": round(sum(d["pnl"]) / d["n"], 4) if d["n"] else 0,
        })

    # Per-EDGE breakdown (the key learning signal — which strategy actually wins)
    by_edge = {}
    for t in paper:
        e = t.get("edge") or ("loose" if t.get("loose") else "validated")
        e = by_edge.setdefault(e, {"wins": 0, "n": 0, "pnl": 0.0, "symbols": set()})
        e["n"] += 1
        e["pnl"] += t.get("pnl", 0)
        e["symbols"].add(t.get("symbol", "?"))
        if t.get("pnl", 0) > 0:
            e["wins"] += 1
    edge_rows = []
    for e, d in sorted(by_edge.items(), key=lambda kv: -kv[1]["n"]):
        edge_rows.append({
            "edge": e, "n": d["n"],
            "win_rate": round(d["wins"] / d["n"] * 100, 1) if d["n"] else 0,
            "avg_pnl": round(d["pnl"] / d["n"], 4) if d["n"] else 0,
            "net_pnl": round(d["pnl"], 4),
            "symbols": sorted(d["symbols"])[:6],
        })

    # Generate suggestions (ranked)
    edge_meta = _edge_freshness(paper)
    suggestions = _suggest(paper_stats, live_stats, regime_rows, edge_rows, edge_meta)

    # EDGE-TAG RECENCY (added 2026-10-10). The per-edge section above is the
    # loop's "accuracy-improvement core", but it is only as fresh as the most
    # recent trade that CARRIES an `edge` tag. Found on 2026-10-10: the last
    # edge-tagged trade in unified-paper.json was 2026-09-13, while the book ran
    # to 2026-10-10 — the two ACTIVE openers (validated + loose) wrote no edge
    # field, so ~87% of trades fell back to loose/validated and the "DECAYING
    # edge 'supertrend'" verdicts were computed on a ~4-week-stale 362-trade
    # subset. Report the freshness explicitly so a frozen edge feed is LOUD,
    # never a silent stale verdict. (Openers now stamp edge; this guards the
    # whole class of "the feed stopped and nobody noticed".)
    return {
        "generated_at_utc": now,
        "paper": paper_stats,
        "live": live_stats,
        "by_regime": regime_rows,
        "by_edge": edge_rows,
        "edge_meta": edge_meta,
        "suggestions": suggestions,
    }


def _edge_freshness(trades: list) -> dict:
    """How fresh is the per-edge attribution, and how stale is the regime feed?

    Returns {'tagged': n, 'tagged_pct': %, 'last_tagged_utc': iso|None,
             'stale_days': float|None, 'stale': bool, 'note': str}
    """
    tagged = [t for t in trades if t.get("edge")]
    out = {"tagged": len(tagged),
           "tagged_pct": round(len(tagged) / len(trades) * 100, 1) if trades else 0.0,
           "last_tagged_utc": None, "stale_days": None, "stale": False, "note": ""}
    if tagged:
        stamps = sorted(t.get("ts_utc", "") for t in tagged)
        last = stamps[-1] or None
        out["last_tagged_utc"] = last
        if last:
            try:
                dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
                out["stale_days"] = round(days, 1)
                out["stale"] = days > 2.0
            except Exception:  # noqa: BLE001 — a bad stamp must not crash the loop
                pass
    if out["tagged_pct"] < 50 or out["stale"]:
        out["note"] = (f"per-edge stats are NOT current: {out['tagged']}/{len(trades)} "
                       f"trades tagged ({out['tagged_pct']}%), last tag "
                       f"{out['last_tagged_utc']} ({out['stale_days']}d ago). "
                       f"Edge promote/demote verdicts below are STALE — read them "
                       f"as historical, not as the live book.")
    return out


def _suggest(paper, live, regimes, edges=None, edge_meta=None):
    out = []
    edges = edges or []
    # 0. EDGE-FEED FRESHNESS (2026-10-10). If the per-edge attribution has
    # stopped updating, EVERY per-edge verdict below is stale and must not be
    # read as the live state of the book. This is the guard for the defect
    # found today: the two active openers wrote no `edge` tag for ~4 weeks, so
    # the loop's "core" section reported a frozen subset as if it were current.
    if edge_meta and edge_meta.get("stale"):
        out.append({
            "priority": 1, "type": "stale_edge_data",
            "idea": (f"STALE per-edge feed: last edge-tagged trade "
                     f"{edge_meta['last_tagged_utc']} ({edge_meta['stale_days']}d ago), "
                     f"only {edge_meta['tagged_pct']}% of trades tagged. Per-edge "
                     f"promote/demote verdicts below are HISTORICAL — do not act on "
                     f"them as current. Fix the opener tagging before trusting them."),
        })
    # 1. Sample size gate
    if not paper or paper["n"] < 20:
        out.append({
            "priority": 1, "type": "wait_for_data",
            "idea": "Not enough trades yet (need ~30) to trust any tuning. Keep paper running; do not change params on <30 trades.",
        })
        out.sort(key=lambda s: s["priority"])
        return out
    # 2. Edge drift
    if paper["win_rate"] < 55:
        out.append({
            "priority": 1, "type": "edge_decay",
            "idea": f"Paper win rate {paper['win_rate']}% below 55% — the capitulation edge may be decaying. Trigger a walk-forward re-fit and consider tightening RSI or vol thresholds.",
        })
    elif paper["win_rate"] >= 65:
        out.append({
            "priority": 2, "type": "edge_confirmed",
            "idea": f"Paper win rate {paper['win_rate']}% confirms the ~70% target. Candidate to graduate to live at $5 once 30+ trades logged.",
        })
    # 3. Profit factor
    if paper and paper["profit_factor"] < 1.5 and paper["n"] >= 20:
        out.append({
            "priority": 2, "type": "rr_tune",
            "idea": f"Profit factor {paper['profit_factor']} < 1.5. Consider raising TP:SL ratio (2:1 -> 2.5:1) or tightening the stop.",
        })
    # 4. Per-regime decay.
    # GUARD (2026-10-10): the paper book records NO regime field (verified:
    # 0/2778 trades carry `regime`), so by_regime collapses to a single "?"
    # bucket holding the WHOLE book. A "Regime ? at 47.8% — stand down" line is
    # not a regime signal; it just restates the book's own win rate with a
    # question mark, and cannot be acted on. Emit an honest data-gap note
    # instead of a fake actionable one. (The judge computes a real regime per
    # cycle but the paper openers do not persist it — stamping it is the fix.)
    unknown_regime = [r for r in regimes if r["regime"] == "?"]
    named_regime = [r for r in regimes if r["regime"] != "?"]
    if unknown_regime and not named_regime:
        out.append({
            "priority": 3, "type": "regime_data_gap",
            "idea": ("NO regime attribution on paper trades (all "
                     f"{unknown_regime[0]['n']} fall in '?'), so per-regime decay "
                     "cannot be read. The judge computes a regime each cycle but the "
                     "paper openers do not persist it. Regime analysis is DISABLED "
                     "until trades carry a regime tag — do not treat the '?' line as "
                     "a stand-down signal."),
        })
    for r in named_regime:
        if r["n"] >= 10 and r["win_rate"] < 50:
            out.append({
                "priority": 3, "type": "regime_disable",
                "idea": f"Regime {r['regime']} at {r['win_rate']}% over {r['n']} trades — consider standing down in this regime.",
            })
    # 4b. PER-EDGE promotion/demotion — the accuracy-improvement core.
    # Edges with n>=10 that hold WR>=58% + positive EV graduate toward real;
    # edges that decay below 45% get flagged for threshold tightening or removal.
    for e in edges:
        if e["n"] < 10:
            continue
        if e["win_rate"] >= 58 and e["avg_pnl"] > 0:
            out.append({
                "priority": 2, "type": "edge_promote",
                "idea": f"Edge '{e['edge']}' PROVEN: {e['win_rate']}% WR / {e['n']} trades / +${e['net_pnl']:.2f} net on {','.join(e['symbols'][:4])}. Keep trading real on this; it carries the account.",
            })
        elif e["win_rate"] < 45:
            out.append({
                "priority": 2, "type": "edge_decay",
                "idea": f"Edge '{e['edge']}' DECAYING: {e['win_rate']}% WR / {e['n']} trades / {e['net_pnl']:+.2f} net. Tighten its entry (deeper dip / lower RSI) or stand it down — it is losing edge.",
            })
    # 5. Unique/outside-the-box ideas (always add a couple)
    # TESTED-NEGATIVE (2026-10-10): the volume-spike idea below was run through
    # hunt.py's IS/OOS funnel (dip_vol + brk_vol families, 24 candidates, at
    # exchange cost). 0 of 24 beat PF 1.0 in the screen window, so none reached
    # IS/OOS. Evidence: docs/proposals/volume-gate-negative.md and
    # reports/hunt/2026-10-10T011755Z_15m.json. Reported as a closed result, not
    # re-proposed as an open idea, so the loop stops repeating it.
    out.append({
        "priority": 4, "type": "explore_result",
        "idea": "CLOSED (tested NEGATIVE 2026-10-10): volume-spike confirmation "
                "(volume z-score > 2 as an extra gate) adds no edge — 0/24 "
                "candidates beat PF 1.0 at cost. See "
                "docs/proposals/volume-gate-negative.md. Do not retest this form.",
    })
    out.append({
        "priority": 4, "type": "explore",
        "idea": "Explore: time-of-day filter — capitulation bounces may cluster around US session open (13:30-16:00 UTC). Test if a session filter raises win rate.",
    })
    out.sort(key=lambda s: s["priority"])
    return out


def main():
    report = analyze()
    out_path = ROOT / "reports" / "improvement-report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
