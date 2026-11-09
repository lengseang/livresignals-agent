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
from datetime import datetime, timedelta, timezone
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
except Exception as _e:  # noqa: BLE001 — never let the reader import kill the loop
    _collapse_multitf = None
    _collapse_import_error = f"{type(_e).__name__}: {_e}"
else:
    _collapse_import_error = ""
try:
    from paper_book import edge_liveness as _edge_liveness
except Exception:  # noqa: BLE001 — never let the reader import kill the loop
    _edge_liveness = None

# PROFIT-FACTOR FLOOR, ON NET (2026-10-22)
# The old rule 3 used 1.5 on `paper["profit_factor"]`, which is a NET ratio.
# 1.5 was written for a GROSS book: on a ~2:1 design the measured 0.1545% round
# trip removes ~12% of each win and adds ~24% to each loss, which pulls PF down
# by roughly a third. So 1.5-on-net was a bar the book cleared on ZERO runs
# since the 2026-10-11 net switch — an always-on warning, the failure mode the
# 2026-10-13 tag-freshness fix removed.
#
# The honest net bar is "is this book paying after costs, with any margin?".
# On the measured book PF 1.05 is the LIVE edges earning +$3.44 net over 2365
# trades — thin, but positive and worth keeping. Below 1.00 the book loses
# money. So the flag sits just above break-even: 1.02. A book that pays by a
# margin is measured by the per-edge net rules (which name the edge, its WR and
# its dollars); this aggregate line only asks whether the live book clears its
# own costs at all. Setting it at 1.10 flagged a book that was making money,
# which is the same error as the old 1.5 in the other direction.
PF_NET_FLOOR = 1.02

# Minimum LIVE sample before a PF verdict is drawn. Same 20-trade floor rule 3
# always used; applied now to the live population, which is the one the advice
# would be acted on.
PF_MIN_LIVE_N = 20

# RECENT-WINDOW PF (2026-10-23)
# Rule #15 stopped rule 3 pooling DEAD edges with live ones. It still pooled
# every MONTH the live edges ever traded. Measured that day: the live cohort
# (loose+validated, n=2183) reads PF 1.03 net +2.20 over its whole life — but
# that is 2026-09 paying PF 1.17 / +6.94 plus 2026-10 paying PF 0.82 / -4.81.
# The pooled figure clears PF_NET_FLOOR by 0.01 and so draws NO advice, while
# the last four weeks are the losing part and are getting worse (weekly PF
# 1.53 -> 1.27 -> 0.75 -> 0.75 -> 1.20 -> 0.50). That is rule #12 (a verdict
# must state its window) applied to the one aggregate that still had none:
# a mean over a population with no time bound cannot say whether the book is
# paying NOW, and the older the book the longer a good month can hide a bad one.
#
# The window is 28 days — four scan-cadence weeks, long enough that a single
# quiet week cannot flip the verdict, short enough that it is a statement about
# the current market. Its sample floor is the same PF_MIN_LIVE_N so a thin
# window cannot manufacture advice either.
PF_WINDOW_DAYS = 28

# CLOSED HYPOTHESES (2026-11-05)
# Every exploration the loop has proposed is supposed to end in a verdict. The
# volume-gate idea did — but only because a human edited the literal out of
# `_suggest()` by hand. The time-of-day idea sat in the report UNTOUCHED from the
# first commit until 2026-11-05: a hardcoded "explore" line no run could ever
# close, printed every 40 minutes under the real findings. An un-closeable
# suggestion is not a backlog item, it is a fixed cost the owner pays on every
# read — and it trains the eye to skim priority 4.
#
# So an idea is PROPOSED in `_explorations()` and CLOSED here. A closed key
# renders as an `explore_result` carrying its verdict — never as an open task,
# and never as silence: the point is to keep the negative knowledge while
# stopping the re-run. Each entry must carry evidence, because a verdict nobody
# can check is a rumour with a date on it.
CLOSED_HYPOTHESES: dict[str, dict] = {
    "volume-spike-gate": {
        "verdict": (
            "CLOSED (tested NEGATIVE 2026-10-10): volume-spike confirmation "
            "(volume z-score > 2 as an extra gate) adds no edge — 0/24 "
            "candidates beat PF 1.0 at cost. See "
            "docs/proposals/volume-gate-negative.md. Do not retest this form."),
        "evidence": "docs/proposals/volume-gate-negative.md",
    },
    "session-filter": {
        "verdict": (
            "CLOSED (tested NEGATIVE 2026-11-05): no UTC session band survives "
            "on the costed paper book (n=812, net -3.48). Asia t=-2.06 pooled "
            "but flips to +0.15 in the second half; NY's +0.52 is one day "
            "(2026-10-18, +3.21) and reads t=-0.12 without it; London's rise is "
            "an artefact of removing that day and flips sign across its own "
            "halves. No session filter proposed. Re-open only with a "
            "pre-declared band set plus a walk-forward split that a band must "
            "survive in BOTH halves. See "
            "docs/proposals/session-filter-closed-2026-11-05.md."),
        "evidence": "docs/proposals/session-filter-closed-2026-11-05.md",
    },
}


def _explorations():
    """The single list of explorations this loop knows about.

    Every entry is `(key, open_text)`. Whether it is emitted as an OPEN
    suggestion or as a CLOSED result is decided by CLOSED_HYPOTHESES alone —
    the list here says what the loop has considered, the register says where
    each one landed. Adding an idea = one line here; closing it = one entry in
    the register. Neither requires editing the other.
    """
    return [
        ("volume-spike-gate",
         "Explore: volume-spike confirmation (volume z-score > 2 as an extra "
         "gate) — does demanding volume confirm the dip?"),
        ("session-filter",
         "Explore: time-of-day filter — do capitulation bounces cluster in a "
         "UTC session, and does a session filter raise net win rate?"),
    ]


def _emit_explorations(hypotheses) -> list:
    """Render each hypothesis as an open `explore` or a closed `explore_result`.

    A key in CLOSED_HYPOTHESES is emitted as `explore_result` carrying its
    verdict — so the owner still sees that it was tested and what the answer
    was (negative knowledge is knowledge, SYSTEM_MAP §6), but never as a live
    to-do. That is the actual defect this closes: the old code could only stop
    repeating an idea by deleting the line, which also deleted the record that
    the idea had been tried.

    The register is the authority: a caller passing a key that is closed gets
    the closed rendering regardless of what text it passed in.
    """
    out = []
    for key, idea in hypotheses:
        closed = CLOSED_HYPOTHESES.get(key)
        if closed is not None:
            out.append({"priority": 4, "type": "explore_result",
                        "idea": closed["verdict"]})
            continue
        out.append({"priority": 4, "type": "explore", "idea": idea})
    return out


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
    else:
        # A reader that silently skips the collapse re-inflates the book with
        # one fill per timeframe — 530 of 3,164 rows on 2026-10-22. The import
        # was already guarded; the SILENCE was the defect. Surface it on the
        # card instead of reporting statistics from the double-counted book.
        print(f"⚠️  MULTI-TF COLLAPSE UNAVAILABLE ({_collapse_import_error}) — "
              f"per-edge/per-regime figures below are computed on a book where "
              f"one fill is counted once per timeframe; do not quote them.")

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
    # it is. The WIN RATE is net for the same reason (2026-10-18): a verdict that
    # reports net dollars beside a gross win rate mixes two units in one
    # sentence, and the gross count inflates exactly the edge that is bleeding.
    # The gross rate is carried separately, under a gross label.
    by_regime = {}
    for t in paper:
        r = by_regime.setdefault(t.get("regime", "?"), {"wins": 0, "wins_gross": 0,
                                                        "n": 0, "pnl": []})
        r["n"] += 1
        g = t.get("pnl", 0) or 0
        if g > 0:
            r["wins_gross"] += 1
        net_t = _pnl_of(t)[0]
        if net_t > 0:
            r["wins"] += 1
        r["pnl"].append(net_t)
    regime_rows = []
    for r, d in by_regime.items():
        regime_rows.append({
            "regime": r, "n": d["n"],
            "win_rate": round(d["wins"] / d["n"] * 100, 1) if d["n"] else 0,
            "win_rate_gross": round(d["wins_gross"] / d["n"] * 100, 1) if d["n"] else 0,
            "avg_pnl": round(sum(d["pnl"]) / d["n"], 4) if d["n"] else 0,
        })

    # Per-EDGE breakdown (the key learning signal — which strategy actually wins).
    # NET-of-cost, so an edge whose gross edge is thinner than the fee is called
    # out as losing rather than promoted. `gross_pnl` is carried for the reader
    # who wants to see the haircut; the promote/demote rules read net_pnl.
    #
    # NOTE (2026-10-18): `win_rate` counts NET wins, like every other figure in
    # the per-edge verdict. It used to count GROSS wins ("if pnl > 0") beside a
    # NET `net_pnl`, so on the real book `validated` read "47.5% WR ... -3.50 net
    # ... DECAYING (net)" while its net win rate was 43.7% — the same edge read
    # 3.8 points healthier than it was, in the one sentence meant to condemn it.
    # `win_rate_gross` keeps the old number under an honest name.
    by_edge = {}
    for t in paper:
        e = t.get("edge") or ("loose" if t.get("loose") else "validated")
        e = by_edge.setdefault(e, {"wins": 0, "wins_gross": 0, "n": 0, "pnl": 0.0,
                                   "gross": 0.0, "symbols": set(),
                                   "win_usd": 0.0, "loss_usd": 0.0})
        e["n"] += 1
        net_t = _pnl_of(t)[0]
        e["pnl"] += net_t
        # Win/loss DOLLARS, kept so a profit factor can be re-formed on any
        # sub-population (2026-10-22). A net sum cannot reconstruct a ratio: an
        # edge at net -0.13 after +55/-55 of trading is not "PF 0", and reading
        # it that way is what let rule 3's figure drift into nonsense.
        if net_t > 0:
            e["win_usd"] += net_t
        else:
            e["loss_usd"] += -net_t
        g = t.get("pnl", 0) or 0
        e["gross"] += g
        e["symbols"].add(t.get("symbol", "?"))
        if g > 0:
            e["wins_gross"] += 1
        if net_t > 0:
            e["wins"] += 1
    edge_rows = []
    for e, d in sorted(by_edge.items(), key=lambda kv: -kv[1]["n"]):
        edge_rows.append({
            "edge": e, "n": d["n"],
            "win_rate": round(d["wins"] / d["n"] * 100, 1) if d["n"] else 0,
            "win_rate_gross": round(d["wins_gross"] / d["n"] * 100, 1) if d["n"] else 0,
            "avg_pnl": round(d["pnl"] / d["n"], 4) if d["n"] else 0,
            "net_pnl": round(d["pnl"], 4),
            "gross_pnl": round(d["gross"], 4),
            "gross_win": round(d["win_usd"], 4),
            "gross_loss": round(d["loss_usd"], 4),
            "symbols": sorted(d["symbols"])[:6],
        })

    # Generate suggestions (ranked)
    edge_meta = _edge_freshness(paper)
    edge_meta["multitf_collapsed"] = multi_tf_removed
    if _collapse_multitf is None:
        edge_meta["multitf_collapse_error"] = _collapse_import_error or "unavailable"
    # LIVENESS (2026-10-21). `last_fill` per edge: a per-edge verdict computed
    # over the whole book quotes an unbounded mean, so a design that stopped
    # filling weeks ago still draws "tighten it or stand it down". See
    # paper_book.edge_liveness for the measured case (seven edges, all
    # disabled, all silent for 38-46 days, all still getting live-sounding
    # advice). The same field stops a frozen record from being read as proven.
    liveness = _edge_liveness(paper) if _edge_liveness is not None else {}
    edge_meta["last_fill"] = (liveness or {}).get("last_fill", {})
    edge_meta["liveness"] = (liveness or {}).get("stats", {})
    # WINDOWED PF (2026-10-23). Attached BEFORE _suggest so rule 3 can judge the
    # book on the market it is trading in, not on its lifetime mean. Computed on
    # the collapsed `paper` list — the same population every other figure uses.
    edge_meta["recent_pf"] = _recent_live_pf(paper, edge_meta)
    # PER-EDGE RECENT NET (2026-11-08). `recent_pf` answers the aggregate
    # question for the LIVE cohort. It cannot annotate a STOPPED edge, and on
    # 2026-11-08 that gap produced the most misleading line in the report:
    #
    #     "loose (n=1261, +3.37 net, last fill 2026-10-11) — STOPPED"
    #
    # `loose` is the book's only net-POSITIVE cohort over its life, so that
    # line reads "we switched off our only winner". Measured 2026-11-09 on the
    # collapsed book: `loose` was -2.34 net over its last 7 days and -5.54 over
    # its last 14 — it was collapsing when the engine stood it down, and its
    # lifetime +6.25 is two good September weeks carried as a mean. NOTE the
    # 28d anchored window is +4.55, so the clause shown beside it does NOT by
    # itself prove the stop was right; it only stops the lifetime mean being
    # read alone. The earlier "n=17, -0.20 in the last 28d" figure was the
    # self_improve disable-gate cohort, not a 28d window — do not quote it as one.
    # A lifetime mean quoted beside the word STOPPED, with no window, invites
    # the owner to re-enable a loser — the exact inverse of the harm the
    # windowed-PF rule (#12/#16, PF_WINDOW_DAYS) was built to prevent, applied
    # to the per-edge line instead of the aggregate. Every dead/stopped edge
    # now carries its recent net, so a positive lifetime figure can never be
    # read without the number that justified stopping it.
    # ANCHORED AT EACH EDGE'S OWN LAST FILL (2026-11-09, T0.16). The 2026-11-08
    # version of this line windowed `[now - 28d, now]` for every edge, which is
    # structurally empty for the ONLY population it serves. A STOPPED or DEAD
    # edge has not filled recently by definition: `loose` last filled
    # 2026-10-11T23:16Z, so a 28d window ending 2026-11-09T09:30Z opens
    # 2026-10-12T09:30Z — entirely after the edge's last trade. `loose` was
    # therefore absent from `recent_edge_net` (the only key was `validated`) and
    # `_recent_clause()` returned "" — the shipped sentence appeared in the
    # JOURNAL and a fixture, never in a report. Anchoring each edge's window at
    # its last fill measures the final trading window before it went quiet,
    # which is the window that explains the stop. A live edge fills daily, so
    # its anchor is within hours of now and the number is unchanged in substance.
    edge_meta["stopped"] = _stopped_edges(DATA / "signals-state.json")
    _dead = set(_split_live_pf(edge_rows, edge_meta)["dead"]["edges"]) | \
        set(edge_meta["stopped"])
    _last_fill = (liveness or {}).get("last_fill") or {}
    _anchors = {e: _last_fill[e] for e in _dead if _last_fill.get(e)}
    edge_meta["recent_edge_net"] = _recent_edge_net(
        paper, PF_WINDOW_DAYS, anchors=_anchors)
    edge_meta["redundant"] = (liveness or {}).get("redundant", {})
    # STOPPED EDGES (2026-11-08). `edge_liveness` only knows WHEN an edge last
    # filled; it cannot know whether the silence is a decision. The writer's
    # disable list is `data/signals-state.json` `disabled` — the same list every
    # opener goes through via `unified_trader._edge_disabled` — and on
    # 2026-11-08 it held `loose`, whose last fill was 2026-10-11. The note called
    # that "no fill in >14d ... Re-enable ONE at a time", which is right for a
    # design nobody switched off and wrong for one that self_improve already
    # stood down. Different next action, so the note must name the right one.
    suggestions = _suggest(paper_stats, live_stats, regime_rows, edge_rows,
                           edge_meta)

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


def _edge_is_live(e: str, edge_meta: dict | None,
                  stale_days: float = 14.0) -> bool:
    """Has this edge produced a fill inside `stale_days`?

    No liveness data at all (reader failed to import, empty book) is treated as
    LIVE, so a broken reader degrades to the old whole-book behaviour rather
    than silently muting every verdict. An edge with a fill stamp it cannot
    parse is also treated as live — the guard only silences an edge it can
    positively date.
    """
    lf = ((edge_meta or {}).get("last_fill") or {}).get(e)
    if not lf:
        return True
    try:
        dt = datetime.fromisoformat(str(lf).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    age = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    return age <= stale_days


def _stopped_edges(state_path: Path) -> dict:
    """{edge: {"since": iso|None, "source": name}} for the writer's disable list.

    `signals-state.json` `disabled` holds a bare list of edge names — no date.
    The date is not invented here (rule: never write a synthetic number); the
    caller renders whatever this returns, and `since` is None when the file
    does not carry one. A missing/unreadable file returns {}: the note then
    degrades to the pre-2026-11-08 "no fill in >14d" wording rather than
    claiming every dead edge was switched off.
    """
    try:
        st = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    disabled = st.get("disabled") or []
    if not isinstance(disabled, list):
        return {}
    return {str(e): {"since": None, "source": state_path.name}
            for e in disabled if e}


def _split_live_pf(edges: list, edge_meta: dict | None) -> dict:
    """Split the book into LIVE and NOT-LIVE populations, with each net PF.

    WHY (2026-10-22)
    ----------------
    Rule 3 asked the DEAD half of the book how to tune the LIVE half's exits.
    `paper_stats["profit_factor"]` is computed over every closed record, so the
    seven edges that `edge_not_live` had just declared HISTORY (272 trades,
    -$5.68 net, net PF 0.47) were pooled with the two edges still trading
    (2365 trades, +$3.44 net, net PF 1.05). The pooled figure, 0.97, came out
    BELOW 1.0 and drew "raise TP:SL to 2.5:1 or tighten the stop" — advice
    about exits nobody is taking, drawn from losses nobody is able to repeat.
    It is the 2026-10-21 liveness rule (#14) applied to the last place that
    still pooled the whole book: a rule's own premise must be computed over the
    same population the rule is allowed to speak about.

    PF is recomputed from win/loss DOLLARS, never from a net sum — an edge at
    net -0.13 after +29.14/-29.27 is PF 0.995, not "PF 0". `gross_win` and
    `gross_loss` are emitted per row by `analyze`. A row carrying only
    `net_pnl` (an older caller) donates that value to the side its sign
    implies, flagged `exact: False` so the degradation is visible.

    `edge_meta` may be the full dict or a bare {edge: last_fill} map: passing
    the bare map once made every edge undatable -> "live" -> the split silently
    collapsed back to the whole book, i.e. straight back into the defect.
    """
    if edge_meta and "last_fill" not in edge_meta:
        edge_meta = {"last_fill": edge_meta}

    def _pf(rows):
        # A row's net is the DIFFERENCE of its win/loss dollars — it says
        # nothing about which side to read. Filtering rows by the sign of their
        # net silently drops the +29.14 win side of a row whose net is -0.13.
        def _amt(r, win):
            if "gross_win" in r and "gross_loss" in r:
                return r["gross_win" if win else "gross_loss"] or 0
            net = r.get("net_pnl", 0) or 0          # older row: sign is all we have
            return net if (net > 0) == win else 0

        gw = abs(sum(_amt(r, True) for r in rows))
        gl = abs(sum(_amt(r, False) for r in rows))
        pf = (gw / gl) if gl > 0 else (None if gw > 0 else 0.0)
        return {"n": sum(r.get("n", 0) or 0 for r in rows),
                "net": round(sum(r.get("net_pnl", 0) or 0 for r in rows), 4),
                "pf": round(pf, 2) if pf is not None else None,
                "exact": all("gross_win" in r and "gross_loss" in r for r in rows),
                "edges": sorted(r["edge"] for r in rows)}

    live_rows = [r for r in edges if _edge_is_live(r["edge"], edge_meta)]
    dead_rows = [r for r in edges if not _edge_is_live(r["edge"], edge_meta)]
    return {"live": _pf(live_rows), "dead": _pf(dead_rows), "whole": _pf(edges),
            "has_liveness": bool((edge_meta or {}).get("last_fill"))}


def _recent_live_pf(trades: list, edge_meta: dict | None,
                    days: int = PF_WINDOW_DAYS, now=None) -> dict:
    """Net PF of the LIVE edges over just the last `days`, with its window stated.

    WHY (2026-10-23)
    ----------------
    #15 fixed WHICH edges the aggregate asks (live, not corpses). It left the
    aggregate asking them about their WHOLE LIFE. Measured: the live cohort
    (loose+validated) is PF 1.03 over n=2183 — but that mean is 2026-09 paying
    1.17/+6.94 plus 2026-10 paying 0.82/-4.81. Because 1.03 clears the 1.02
    floor by a single basis point, rule 3 stayed SILENT while the last four
    weeks lost $4.81, and the weekly series was still falling
    (1.53, 1.27, 0.75, 0.75, 1.20, 0.50). A pooled mean cannot go red on the
    month that matters; the older the book, the longer one good month hides a
    bad one. Same defect class as #12 (window) and #9 (re-derive now), applied
    to time rather than to data or to a threshold.

    This function does not REPLACE the pooled figure — it is returned beside it
    (see `_pf_advice`) so the reader can see the book paying over its life and
    bleeding in the window. It computes over raw trades (which carry `ts_utc`),
    not edge rows, because the edge rows have no timestamps.

    Trades with an unparseable/missing ts_utc are EXCLUDED and counted, never
    silently dated to "now" — a record that cannot be placed in time must not be
    allowed to vote on a windowed verdict.
    """
    if not edge_meta or not edge_meta.get("last_fill"):
        return {"available": False, "reason": "no liveness data", "days": days}

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    win, undated = [], 0
    for t in trades:
        edge = t.get("edge") or ("loose" if t.get("loose") else "validated")
        if not _edge_is_live(edge, edge_meta):
            continue
        raw = t.get("ts_utc")
        if not raw:
            undated += 1
            continue
        try:
            ts = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            undated += 1
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            win.append(t)

    gw = gl = 0.0
    for t in win:
        v = _pnl_of_trade(t)
        if v > 0:
            gw += v
        else:
            gl += -v
    pf = (gw / gl) if gl > 0 else (None if gw > 0 else 0.0)
    return {"available": True, "days": days, "n": len(win),
            "net": round(gw - gl, 4),
            "pf": round(pf, 2) if pf is not None else None,
            "undated": undated,
            "start_utc": cutoff.isoformat()}


def _pnl_of_trade(t: dict) -> float:
    """NET-of-cost pnl for one paper trade (module-level twin of analyze's local
    helper, so windowed helpers can be unit-tested without running analyze)."""
    if _paper_net_of is None:
        return t.get("pnl", 0) or 0
    try:
        return _paper_net_of(t)[0]
    except Exception:  # noqa: BLE001 — a reader must not kill the loop
        return t.get("pnl", 0) or 0


def _recent_edge_net(trades: list, days: int = PF_WINDOW_DAYS, now=None,
                     anchors: dict | None = None) -> dict:
    """{edge: {"n", "net", "days", "start_utc", "end_utc", "anchored"}} per edge.

    WHY (2026-11-08)
    ----------------
    `_recent_live_pf` windows the aggregate over the LIVE cohort. The per-edge
    history note (§4c) names DEAD and STOPPED edges, which that helper skips by
    construction — so the only number it could print for them was the lifetime
    mean. On 2026-11-08 that read `loose (n=1261, +3.37 net) — STOPPED`, i.e.
    "the only profitable edge was switched off", when the window that got it
    stopped was negative. A positive lifetime mean next to STOPPED is an
    invitation to re-enable a loser; the recent net is the number that
    justified the stop, so it must travel with the line.

    WHY AN ANCHOR (2026-11-09) — the 2026-11-08 version did not work
    ------------------------------------------------------------------
    The first version windowed `[now - days, now]` for every edge. A STOPPED
    edge by definition has not filled recently: `loose` last filled
    2026-10-11T23:16Z, and a 28d window ending 2026-11-09T09:30Z opens
    2026-10-12T09:30Z — ENTIRELY after the edge's last trade. So `loose` had
    0 fills, was absent from the returned dict, and the clause silently
    degraded to the pre-fix wording. Measured on the live book: the only key
    was `validated`. A now-anchored window can never contain the population
    it was written for; the harder the stop, the more certainly it is empty.

    `anchors` = {edge: iso_ts} (typically the edge's last fill). An anchored
    edge is measured over `[anchor - days, anchor]` — the final trading window
    before it went quiet, which is the window that explains the decision. An
    edge with no anchor keeps the now-anchored meaning exactly, so every
    pre-existing caller and test is unchanged.

    Attribution follows the one book rule (`edge` tag, else `loose`, else
    `validated`) — the same rule `edge_liveness` and `self_improve` use, so
    this cannot disagree with the split about who owns a fill.

    Trades with an unparseable/missing `ts_utc` are excluded, never dated to
    "now" — the same rule as `_recent_live_pf`: a record that cannot be placed
    in time must not vote on a windowed verdict.
    """
    now = now or datetime.now(timezone.utc)
    anchors = anchors or {}

    def _parse(raw):
        if not raw:
            return None
        try:
            ts = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    out: dict[str, dict] = {}
    for t in trades:
        edge = t.get("edge") or ("loose" if t.get("loose") else "validated")
        ts = _parse(t.get("ts_utc"))
        if ts is None:
            continue
        # An anchor that cannot be dated must not silently become "now": fall
        # back to the now-anchored window, which is at least a stated rule.
        end, anchored = now, False
        anchor_ts = _parse(anchors.get(edge))
        if anchor_ts is not None:
            end, anchored = anchor_ts, True
        cutoff = end - timedelta(days=days)
        if ts < cutoff or ts > end:
            continue
        d = out.setdefault(edge, {"n": 0, "net": 0.0, "days": days,
                                  "start_utc": cutoff.isoformat(),
                                  "end_utc": end.isoformat(),
                                  "anchored": anchored})
        d["n"] += 1
        d["net"] += _pnl_of_trade(t)
    for d in out.values():
        d["net"] = round(d["net"], 4)
    return out


def _suggest(paper, live, regimes, edges=None, edge_meta=None):
    out = []
    edges = edges or []
    last_fill = (edge_meta or {}).get("last_fill") or {}
    redundant = (edge_meta or {}).get("redundant") or {}
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
        # 2026-11-05: this used to `return out` here, which silently dropped
        # section 5 — every exploration AND every closure record. A thin book
        # says nothing about a hypothesis that was already tested on a full
        # one, so the wait_for_data verdict must not delete that knowledge.
        # The sections between here and section 5 read per-edge/per-regime
        # statistics and are correctly skipped; the exploration list is not
        # a statistic about this book.
        out.extend(_emit_explorations(_explorations()))
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
    # 3. Profit factor — ON THE LIVE POPULATION (rewritten 2026-10-22).
    #
    # The old line read `paper["profit_factor"] < 1.5`, where `paper` is the
    # WHOLE book including every edge rule 4b/4c below has just declared not
    # live. Measured that day: whole book n=2637 PF 0.97 net -2.24, but the
    # live edges were n=2365 PF 1.05 net +3.44 and the seven dead edges were
    # n=272 PF 0.47 net -5.68. The 0.97 was manufactured by pooling corpses,
    # and it read BELOW 1.0 — "your book loses before costs", which was false
    # for every trade that could still be taken. A rule may only quote a
    # population it is entitled to act on: the same reason #14 exists.
    #
    # While restructuring it, the bar was also re-derived: 1.5 was a GROSS
    # threshold applied to a NET ratio since 2026-10-11, so it was unreachable
    # and therefore always-on. See PF_NET_FLOOR.
    split = _split_live_pf(edges, edge_meta)
    pop = split["live"] if split["has_liveness"] else split["whole"]
    # RECENT WINDOW (2026-10-23): the pooled live book answers "has this cohort
    # paid over its life?", which is NOT the question exit tuning asks. A book
    # that cleared its floor on one good month and has bled every week since
    # draws no advice from a lifetime mean — see _recent_live_pf. `recent` is
    # supplied by analyze(); when absent (older callers / unit tests) the rule
    # degrades to the pooled behaviour it had before, and says so by omission.
    recent = (edge_meta or {}).get("recent_pf") or {}
    recent_votes = (recent.get("available") and recent.get("n", 0) >= PF_MIN_LIVE_N
                    and recent.get("pf") is not None
                    and recent["pf"] < PF_NET_FLOOR)
    pooled_votes = (pop["n"] >= PF_MIN_LIVE_N and pop["pf"] is not None
                    and pop["pf"] < PF_NET_FLOOR)
    # PF is None only when there are no losing dollars to divide by — undefined,
    # never "0.00, catastrophically thin" — so it draws no advice.
    if pooled_votes or recent_votes:
        # NAME THE WHOLE COHORT, not just the rows that carried a PF (2026-11-08).
        # `pop["edges"]` is the split's live set and already includes an edge
        # whose PF is None. But the sentence below is the only place a reader
        # learns WHO was judged, and the exclusion note that follows lists the
        # dead ones — so an edge present in one and absent from the other reads
        # as dead. Naming the cohort from the split, and the note from the
        # split, keeps the two from contradicting each other.
        names = ", ".join(pop["edges"])
        basis = "live" if split["has_liveness"] else "whole book (no liveness data)"
        if recent_votes:
            # The window is the verdict. State BOTH so the reader can see the
            # gap between "paid over its life" and "losing now" — quoting only
            # one of them is what hid this for four weeks.
            idea = (f"NET profit factor {recent['pf']:.2f} < {PF_NET_FLOOR:.2f} over "
                    f"the LAST {recent['days']}d window ({recent['n']} trades, "
                    f"{recent['net']:+.2f} net) on the {basis} edges ({names}); "
                    f"the same cohort reads {pop['pf']:.2f} over its whole life "
                    f"({pop['n']} trades, {pop['net']:+.2f} net). The book is "
                    f"LOSING NOW and the lifetime mean is masking it. Costs are "
                    f"eating the book — raise TP:SL, tighten the stop, or "
                    f"stand the weakest live edge down.")
        else:
            idea = (f"NET profit factor {pop['pf']:.2f} < {PF_NET_FLOOR:.2f} on the "
                    f"{basis}: {pop['n']} trades, {pop['net']:+.2f} net, "
                    f"{len(pop['edges'])} edge(s) ({names}). Costs are eating the "
                    f"book. Consider raising TP:SL or tightening the stop.")
        # Say what was EXCLUDED and why — a suppression must leave a trace
        # (same rule as the edge_not_live note). Only when it is material.
        dead = split["dead"]
        if split["has_liveness"] and dead["n"] >= PF_MIN_LIVE_N:
            idea += (f" NOT counted: {dead['n']} trades from "
                     f"{len(dead['edges'])} edge(s) with no fill in >14d "
                     f"({dead['net']:+.2f} net) — history, not the live book.")
        out.append({"priority": 2, "type": "rr_tune", "idea": idea})
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
        # LIVENESS GATE (2026-10-21). A verdict over the whole book has no time
        # bound, so an edge that stopped filling weeks ago still reads as a
        # live design that "is losing edge". Measured: seven edges, all
        # disabled, all silent 38-46 days, each drawing "tighten its entry or
        # stand it down" on every run. Standing down an edge that cannot open
        # is a no-op dressed as an action. So a stale edge gets ONE history
        # note (below), never a live tighten/stand-down instruction.
        live = _edge_is_live(e["edge"], edge_meta)
        if not live:
            continue
        # A redundant copy's evidence is its parent's evidence, re-labelled.
        # Emitting a second, independent verdict on it double-counts one
        # experiment — and the copy is not a separate thing to tune.
        if e["edge"] in redundant:
            continue
        # PREVENTIVE, NOT PROVEN-BAD (2026-10-18). The WR floor is a HEURISTIC —
        # it says "this win rate is probably too low to pay the fee". When the
        # measured NET says the edge PAYS, the heuristic is refuted by the money:
        # on the real book after the net-WR fix, edge 'loose' sits at 44.1% net
        # WR and +4.47 net — the only net-POSITIVE edge in the book — and the
        # floor alone would tell the owner to stand it down. A cheap fee erodes
        # a thin per-trade edge but does not prove one; a large sample that is
        # net-positive IS evidence. So the WR floor only draws a verdict on an
        # edge that is not paying; a net-positive low-WR edge is governed by the
        # untouched PROMOTE bar above (WR>=58) and stays in the "keep measuring"
        # band, which is what "promote slowly" means. The DEMOTE-on-net-loss rule
        # is unaffected: net-negative at any WR still fires.
        if e["win_rate"] >= 58 and e["avg_pnl"] > 0 and not losing:
            out.append({
                "priority": 2, "type": "edge_promote",
                "idea": f"Edge '{e['edge']}' PROVEN (net): {e['win_rate']}% WR / {e['n']} trades / +${e['net_pnl']:.2f} net (gross +${e.get('gross_pnl', 0):.2f}) on {','.join(e['symbols'][:4])}. Keep trading real on this; it carries the account.",
            })
        elif losing or (e["win_rate"] < 45 and not losing):
            # Two routes, two sentences — the verdict must state the condition
            # that actually fired (2026-10-16 lesson):
            #   * net-negative -> a P&L fact, at any win rate;
            #   * low WR but net-positive -> a PREVENTIVE flag on a heuristic
            #     (kept for the owner, at p3: "keep measuring", not "stand it
            #     down"). The guard above keeps the two from being conflated.
            why = (f"{e['win_rate']}% WR / {e['n']} trades / {net_pnl:+.2f} net"
                   f" (gross {e.get('gross_pnl', 0):+.2f})")
            if losing:
                # A net loss is explained by NET, whatever the WR route was:
                # either it wins often (WR above the floor) but pays too much, or
                # part of its apparent win rate was gross wins the fee ate
                # (2026-10-18: win_rate is now net, so this is the true rate).
                why += (f" — net-NEGATIVE despite a {e['win_rate']}% win rate "
                        f"(wins too small, losses too large)")
                verdict = ("Tighten its entry (deeper dip / lower RSI) or stand "
                           "it down — it is losing edge.")
            else:
                why += (" — PREVENTIVE only: the win rate is under the 45% "
                        "floor but the edge is NET-POSITIVE, so the floor is a "
                        "heuristic here, not a verdict")
                verdict = ("Keep measuring; do not act on the win rate alone "
                           "while the edge pays.")
            out.append({
                "priority": 2 if losing else 3, "type": "edge_decay",
                "idea": f"Edge '{e['edge']}' DECAYING (net): {why}. {verdict}",
            })
    # 4c. NAME WHAT WAS SILENCED — a suppressed verdict must leave a trace.
    # Silence is only honest if the reader can see what is silent and why;
    # otherwise "no verdict on supertrend" is indistinguishable from "we
    # forgot supertrend" (the always-on-warning lesson, inverted).
    #
    # WHOSE LIST IS THIS? (2026-11-08) It used to be re-derived here ("every
    # row with n>=10 and no fill in >14d"), INDEPENDENTLY of the split that
    # decided which edges rule 3 and 4b were entitled to judge. On 2026-11-08
    # the two disagreed: the split judged `validated` live and put its -5.34
    # into the aggregate verdict, while this note listed `validated` among the
    # edges with "NO per-edge verdict issued" — the same report, two adjacent
    # bullets, opposite statements about one edge. A suppression note is a
    # claim about the split; it must be COMPUTED FROM the split. `stopped`
    # (the writer's disable list) turns "nobody has filled" into "the writer
    # switched it off", which is a different next action.
    split_live = _split_live_pf(edges, edge_meta)
    dead_names = set(split_live["dead"]["edges"])
    stopped = (edge_meta or {}).get("stopped") or {}
    stale_edges = [(e["edge"], e["n"], e["net_pnl"]) for e in edges
                   if e["edge"] in dead_names]
    if stale_edges:
        parts, stopped_parts = [], []
        # PER-EDGE RECENT NET (2026-11-08): the lifetime mean is not enough on a
        # STOPPED edge — see _recent_edge_net. `recent_edge_net` is optional
        # (older callers/tests omit it) and degrades to the old line.
        recent_edge = (edge_meta or {}).get("recent_edge_net") or {}

        def _recent_clause(name):
            """Text that reconciles a lifetime net with the recent window, or ''
            when the window cannot be computed for this edge."""
            r = recent_edge.get(name)
            if not r or not r.get("n"):
                return ""
            days = r.get("days", PF_WINDOW_DAYS)
            # An ANCHORED window ends at the edge's last fill, not at report
            # time — so "last 28d" would be a false claim about the window
            # (2026-11-09: the 2026-11-08 line asserted a now-ending window on
            # a number measured over an earlier one, and on a STOPPED edge the
            # now-ending window was empty anyway, so it printed nothing at all).
            if r.get("anchored"):
                return (f"; {days}d to last fill: n={r['n']}, {r['net']:+.2f} net")
            return (f"; last {days}d: n={r['n']}, {r['net']:+.2f} net")

        for name, n, net in sorted(stale_edges, key=lambda x: x[2]):
            lf = str(last_fill.get(name, ""))[:10]
            # A POSITIVE lifetime mean on a dead edge is the trap: it reads as
            # "we switched off a winner". Say what it did in the window that got
            # it stopped (measured: loose +3.37 life / -0.20 in 28d). Negative
            # lifetime edges gain nothing — no window is needed to believe a loser.
            recent = _recent_clause(name) if net > 0 else ""
            line = (f"{name} (n={n}, {net:+.2f} net, "
                    f"last fill {lf or '?'}{recent})")
            if name in stopped:
                src = (stopped.get(name) or {}).get("source") or "the disable list"
                since = (stopped.get(name) or {}).get("since")
                stopped_parts.append(line + (f" — STOPPED in {src}"
                                             + (f" since {since}" if since else "")))
            else:
                parts.append(line)
        # The header is a claim about EACH line under it, so it may only say
        # ">14d" for lines that are actually >14d old. The note follows the
        # split, and the split's dead set is not guaranteed to be the >14d set
        # (a test pins that disagreement); a note that copies the header's
        # reason onto every line can print "no fill in >14d ... last fill
        # 2026-11-06" — 2 days — which is the same class of defect (a verdict
        # narrated with a reason that did not fire) this note exists to avoid.
        idea = ""
        if parts:
            idea = ("NOT LIVE — no per-edge verdict issued (excluded from the "
                    "verdict above): " + "; ".join(parts) + ". These are "
                    "HISTORY, not the live book: they cannot open, so 'tighten "
                    "it' or 'stand it down' would be a no-op.")
        if stopped_parts:
            idea += (" " if idea else "") + (
                "STOPPED — switched off by the engine, not merely idle: "
                + "; ".join(stopped_parts)
                + ". Standing down an already-disabled edge is a no-op.")
        if idea:
            idea += " Re-enable ONE at a time and let it fill for 14d before judging it."
            out.append({"priority": 3, "type": "edge_not_live", "idea": idea})
    if redundant:
        pairs = [f"{e} ~ {parent}" for e, parent in sorted(redundant.items())]
        out.append({
            "priority": 3, "type": "edge_redundant",
            "idea": ("REDUNDANT — copy of another edge's handoff, no better "
                     "net/WR on a checkable sample: " + "; ".join(pairs)
                     + ". One experiment wearing two names: do not count as "
                     "independent confirmation, and do not tune the copy "
                     "separately from its parent."),
        })
    # 5. Explorations — proposed in _explorations(), closed in CLOSED_HYPOTHESES.
    # The volume-gate run's raw funnel output lives at
    # reports/hunt/2026-10-10T011755Z_15m.json (24 candidates, 0 beat PF 1.0).
    out.extend(_emit_explorations(_explorations()))
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
