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

# The paper book's honest net-of-cost reader. Imported rather than re-derived so
# this loop cannot drift from the writer (unified_trader) or from self_improve,
# which already reports NET. See scripts/paper_net.py for the missing-field trap.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from paper_net import net_of as _paper_net_of, PAPER_COST_RT as _PAPER_COST_RT
except Exception:  # noqa: BLE001 — never let the reader import kill the loop
    _paper_net_of = None
    _PAPER_COST_RT = None
try:
    from paper_book import collapse_multitf as _collapse_multitf
except Exception:  # noqa: BLE001 — never let the reader import kill the loop
    _collapse_multitf = None


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

    # MULTI-TIMEFRAME COLLAPSE (2026-10-15). The paper scan opens one position
    # per (symbol, timeframe), and the same dip agrees on 5m/15m/30m, so one
    # physical trade is written several times. Those copies share an exact entry
    # and pnl but differ in `ts_utc` (sub-second), so no identity key built on
    # time can merge them — and every per-edge / per-regime figure below was
    # computed on the inflated union. Collapse to one row per fill. See
    # scripts/paper_book.py and docs/proposals/paper-multitf-dedupe-2026-10-15.md.
    multi_tf_removed = 0
    if _collapse_multitf is not None:
        paper, multi_tf_removed = _collapse_multitf(paper)

    def _pnl_of(t):
        """NET-of-cost pnl for a paper trade, falling back to gross only when
        the cost is genuinely unrecoverable (and flagging that in the report).

        Reads the same basis as self_improve / unified_trader via paper_net.
        Falls back to gross if paper_net could not be imported, so a broken
        import degrades to the old behaviour rather than crashing the loop."""
        if _paper_net_of is None:
            return t.get("pnl", 0) or 0, False
        v, costed = _paper_net_of(t)
        return v, costed

    def _stats(trades):
        if not trades:
            return None
        # NET is the number the exchange will actually pay. Since 2026-10-11 the
        # measured round trip (0.16092%) is 5.4x the 0.03% the book used to
        # charge, so a gross headline flips this book from +$19 to a LOSS. Every
        # promote/demote verdict below must be read on NET — gross is carried
        # alongside purely so the haircut is visible, never as the verdict basis.
        pnls = [t.get("pnl", 0) or 0 for t in trades]
        costed = [_pnl_of(t) for t in trades]
        nets = [v for v, _ in costed]
        uncosted = sum(1 for _, ok in costed if not ok)
        netwins = [x for x in nets if x > 0]
        netlosses = [x for x in nets if x <= 0]
        n = len(trades)
        total_pnl = sum(pnls)
        net_total = sum(nets)
        ngp = sum(netwins)
        ngl = abs(sum(netlosses))
        pf = ngp / ngl if ngl > 0 else (float("inf") if ngp > 0 else 0.0)
        return {"n": n, "win_rate": round(len(netwins) / n * 100, 1),
                "win_rate_gross": round(sum(1 for p in pnls if p > 0) / n * 100, 1),
                "ev": round(net_total / n, 4),
                "ev_gross": round(total_pnl / n, 4),
                "profit_factor": round(pf, 2),
                "total_pnl": round(net_total, 4),
                "gross_pnl": round(total_pnl, 4),
                "uncosted": uncosted}

    paper_stats = _stats(paper)
    live_stats = _stats(real)

    # Per-regime breakdown (paper). P&L is NET so a regime that only looks good
    # gross (i.e. its edge is smaller than the round-trip fee) reads as the loss
    # it is. Win-rate stays GROSS-signed for continuity with the by_edge table.
    by_regime = {}
    for t in paper:
        r = by_regime.setdefault(t.get("regime", "?"), {"wins": 0, "n": 0, "pnl": []})
        r["n"] += 1
        if (t.get("pnl", 0) or 0) > 0:
            r["wins"] += 1
        r["pnl"].append(_pnl_of(t)[0])
    regime_rows = []
    for r, d in by_regime.items():
        regime_rows.append({
            "regime": r, "n": d["n"],
            "win_rate": round(d["wins"] / d["n"] * 100, 1) if d["n"] else 0,
            "avg_pnl": round(sum(d["pnl"]) / d["n"], 4) if d["n"] else 0,
        })

    # Per-EDGE breakdown (the key learning signal — which strategy actually wins).
    # NET-of-cost, so an edge whose gross edge is thinner than the fee is called
    # out as losing rather than promoted. `gross_pnl` is carried for the reader
    # who wants to see the haircut; the promote/demote rules read net_pnl.
    by_edge = {}
    for t in paper:
        e = t.get("edge") or ("loose" if t.get("loose") else "validated")
        e = by_edge.setdefault(e, {"wins": 0, "n": 0, "pnl": 0.0, "gross": 0.0,
                                   "symbols": set()})
        e["n"] += 1
        e["pnl"] += _pnl_of(t)[0]
        e["gross"] += t.get("pnl", 0) or 0
        e["symbols"].add(t.get("symbol", "?"))
        if (t.get("pnl", 0) or 0) > 0:
            e["wins"] += 1
    edge_rows = []
    for e, d in sorted(by_edge.items(), key=lambda kv: -kv[1]["n"]):
        edge_rows.append({
            "edge": e, "n": d["n"],
            "win_rate": round(d["wins"] / d["n"] * 100, 1) if d["n"] else 0,
            "avg_pnl": round(d["pnl"] / d["n"], 4) if d["n"] else 0,
            "net_pnl": round(d["pnl"], 4),
            "gross_pnl": round(d["gross"], 4),
            "symbols": sorted(d["symbols"])[:6],
        })

    # Generate suggestions (ranked)
    edge_meta = _edge_freshness(paper)
    edge_meta["multitf_collapsed"] = multi_tf_removed
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
        "pnl_basis": "net",
        "paper_cost_rt": _PAPER_COST_RT,
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
             'stale_days': float|None, 'stale': bool, 'note': str,
             'recent_n': n, 'recent_tagged': n, 'recent_tagged_pct': %}

    WHY THERE ARE TWO TAGGING FIGURES (2026-10-13)
    ----------------------------------------------
    `tagged_pct` is over the WHOLE book and is only ever a reported total. It is
    NOT the staleness input. The book permanently holds a large LEGACY
    population written before the 2026-10-10 fix that made the active openers
    stamp `edge`; those trades can never acquire a tag, so a whole-book rate is
    bounded above by taggable/(taggable+legacy) and shrinks as the book grows.
    On 2026-10-13 that read 499/2923 = 17.1% — permanently below the 50%
    threshold — so the guard emitted a priority-class STALE note on EVERY run
    even while `stale_days` was 0.8 (the live feed was demonstrably healthy).
    A warning that is always on is indistinguishable from no warning, and it
    MASKS the genuine freeze this guard exists to catch.

    The staleness question ("is the feed still being fed?") is a CURRENT
    question, so it is judged on `recent_tagged_pct`: the tagging rate from the
    newest trade that slipped through UNTAGGED onward (inclusive). That boundary
    is the last point at which tagging demonstrably failed, so every legacy
    trade — which lies strictly before it — is excluded, while a fresh untagged
    trade lands *inside* the window and counts against the rate. It is detected
    data-drivenly rather than by a hard-coded date, so it self-maintains if the
    tagging scheme is ever reset. Being inclusive of the boundary trade is what
    stops a single new untagged trade from resetting the window past itself and
    hiding.
    """
    tagged = [t for t in trades if t.get("edge")]
    out = {"tagged": len(tagged),
           "tagged_pct": round(len(tagged) / len(trades) * 100, 1) if trades else 0.0,
           "last_tagged_utc": None, "stale_days": None, "stale": False, "note": "",
           "recent_n": 0, "recent_tagged": 0, "recent_tagged_pct": None}

    # The post-tagging-fix window starts at the newest UNTAGGED trade. Everything
    # strictly before it is history the current writers could not have tagged;
    # everything at or after it should be tagged. If nothing is untagged the
    # whole book is the window.
    #
    # If that window is too small to be meaningful (a lone untagged trade at the
    # very top, or a quiet stretch), fall back to the whole-book rate — the
    # pre-fix behaviour — so the guard can only ever be STRICTER, never falsely
    # quiet. A 1-trade window must not decide anything.
    MIN_RECENT = 30
    untagged = [t for t in trades if not t.get("edge")]
    boundary = max((str(t.get("ts_utc", "")) for t in untagged), default="")
    recent = ([t for t in trades if str(t.get("ts_utc", "")) >= boundary]
              if boundary else list(trades))
    if len(recent) < MIN_RECENT:
        recent = list(trades)
    out["recent_n"] = len(recent)
    out["recent_tagged"] = sum(1 for t in recent if t.get("edge"))
    out["recent_tagged_pct"] = (
        round(out["recent_tagged"] / out["recent_n"] * 100, 1) if out["recent_n"] else None)

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
    # Judge on the RECENT tagging rate. Fall back to the whole-book rate only
    # when the recent window is empty (undefined) — that keeps the pre-fix
    # behaviour in the degenerate case, so the guard can be stricter, never
    # falsely quiet.
    judging_pct = (out["recent_tagged_pct"] if out["recent_tagged_pct"] is not None
                   else out["tagged_pct"])
    if judging_pct < 50 or out["stale"]:
        out["note"] = (f"per-edge stats are NOT current: {out['recent_tagged']}/"
                       f"{out['recent_n']} recent trades tagged "
                       f"({judging_pct}%), last tag {out['last_tagged_utc']} "
                       f"({out['stale_days']}d ago). Edge promote/demote verdicts "
                       f"below are STALE — read them as historical, not as the "
                       f"live book.")
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
    #
    # GUARD (2026-10-16): ONE named regime beside a large "?" bucket is the SAME
    # trap wearing a different mask. The `regime` field first reaches the paper
    # book on 2026-10-10 and, measured 2026-10-16, EVERY tagged trade since then
    # carries `RANGING` (18+54+29+27+14 = 142; no other value recorded). So the
    # RANGING cohort IS the recent period, and "Regime RANGING at 40.8% — stand
    # down" is just the book's last-week win rate relabelled as a regime effect.
    # A single-valued conditioning variable cannot be contrasted against
    # anything, so its 40.8% is not attributable to the regime; the loss may be
    # regime, period, or both and this sample cannot tell them apart. Emitting
    # the stand-down would drive a real decision on zero evidence — exactly what
    # the "?" guard above exists to prevent. A per-regime verdict therefore
    # requires AT LEAST TWO named regimes with a judgeable sample, so the named
    # cohort is a proper subset of the period rather than a synonym for it.
    # (Same principle as the edge-freshness guard's "a 1-trade window must not
    # decide anything": a cohort that is the whole population decides nothing.)
    unknown_regime = [r for r in regimes if r["regime"] == "?"]
    named_regime = [r for r in regimes if r["regime"] != "?"]
    judgeable = [r for r in named_regime if r["n"] >= 10]
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
    elif len(judgeable) < 2:
        # Exactly one regime value observed (or none large enough to judge): the
        # "regime" cohort is a time-period alias, not a contrast. Refuse the
        # per-regime verdict and say so with the numbers that show why.
        only = named_regime[0] if named_regime else None
        if only is not None:
            out.append({
                "priority": 3, "type": "regime_data_gap",
                "idea": (f"Regime attribution covers only ONE value ('{only['regime']}', "
                         f"n={only['n']} at {only['win_rate']}%), while "
                         f"{unknown_regime[0]['n'] if unknown_regime else 0} trades carry no "
                         "regime. A single-valued regime field cannot isolate a regime "
                         "effect — the cohort IS the period it was recorded in, so its "
                         "win rate is not attributable to the regime. NO per-regime "
                         "stand-down verdict is issued until >=2 regimes are observed "
                         "with n>=10 each."),
            })
    if len(judgeable) >= 2:
        for r in judgeable:
            if r["win_rate"] < 50:
                out.append({
                    "priority": 3, "type": "regime_disable",
                    "idea": f"Regime {r['regime']} at {r['win_rate']}% over {r['n']} trades — consider standing down in this regime.",
                })
    # 4b. PER-EDGE promotion/demotion — the accuracy-improvement core.
    # Edges with n>=10 that hold WR>=58% + positive NET EV graduate toward real;
    # edges that decay below 45% get flagged for threshold tightening or removal.
    #
    # NOTE (2026-10-11): these rules read NET (net_pnl / avg_pnl are net of the
    # measured round trip). Before this date they read gross, so an edge whose
    # true edge was smaller than the fee was reported as profitable. `gross_pnl`
    # is carried in each row so the haircut stays visible.
    #
    # NOTE (2026-10-17): the DEMOTE rule used to key on WIN RATE ALONE (WR<45),
    # which left a blind spot between the promote bar (WR>=58) and the demote bar
    # (WR<45) where an edge can lose real money invisibly. Measured on the real
    # book that day: edge 'validated' sat at 48.4% WR, inside the blind spot,
    # and lost $2.81 net over 843 trades — the LARGEST single loss in the book,
    # bigger than every edge the rule DID flag (supertrend -1.85, pullback -1.11,
    # bollinger -1.04, ema-cross -0.83, breakout -0.44) combined — yet it drew no
    # suggestion at all. Win rate and P&L are not the same question: an edge can
    # win half its trades and still bleed if the wins are small and the losses
    # large, which is exactly what a dip opener does when the dip keeps going. A
    # net-negative edge with a real sample is declining regardless of how often
    # it wins, so NET is now judged alongside WR. The promote bar is deliberately
    # NOT relaxed: a net-positive edge below WR 58 earns no promote, it is only
    # spared the decay verdict (the repo's "promote slowly" rule).
    for e in edges:
        if e["n"] < 10:
            continue
        net_pnl = e.get("net_pnl", 0) or 0
        losing = net_pnl < 0
        if e["win_rate"] >= 58 and e["avg_pnl"] > 0 and not losing:
            out.append({
                "priority": 2, "type": "edge_promote",
                "idea": f"Edge '{e['edge']}' PROVEN (net): {e['win_rate']}% WR / {e['n']} trades / +${e['net_pnl']:.2f} net (gross +${e.get('gross_pnl', 0):.2f}) on {','.join(e['symbols'][:4])}. Keep trading real on this; it carries the account.",
            })
        elif e["win_rate"] < 45 or losing:
            why = (f"{e['win_rate']}% WR / {e['n']} trades / {net_pnl:+.2f} net"
                   f" (gross {e.get('gross_pnl', 0):+.2f})")
            if losing and e["win_rate"] >= 45:
                # The blind-spot case: it wins often enough to pass the WR floor
                # but still loses money — say WHY it was caught, so a reader who
                # only sees the win rate understands the verdict.
                why += (f" — net-NEGATIVE despite a {e['win_rate']}% win rate "
                        f"(wins too small, losses too large)")
            out.append({
                "priority": 2, "type": "edge_decay",
                "idea": f"Edge '{e['edge']}' DECAYING (net): {why}. Tighten its entry (deeper dip / lower RSI) or stand it down — it is losing edge.",
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
