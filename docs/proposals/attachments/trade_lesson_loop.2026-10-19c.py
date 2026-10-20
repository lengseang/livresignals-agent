#!/usr/bin/env python3
"""Per-trade lesson loop — every trade teaches, win or lose, forever.

WHAT THIS IS
------------
The owner's instruction: "every trade is a lesson. If it wins, ask why and what
could improve. If it loses, do the same. Infinite loop."

That is a different question from the ones already answered elsewhere in this
repo. `paper_learner.py` refits parameters on historical sweeps.
`trade_judge.py` picks which edge to hunt by regime. Neither does this: take ONE
resolved trade, ask what was true about it, compare it against the population,
and write down what it implies.

THE FOUR QUESTIONS, ASKED OF EVERY TRADE
----------------------------------------
  1. Why did this move the way it did?   (outcome + exit mechanism + geometry)
  2. Was this a GOOD DECISION or a GOOD OUTCOME?
     These are not the same. A trade can win on a bad decision (lucky) and lose
     on a good one (unlucky). Conflating them is how a book learns superstition.
     This is the single most important distinction in the whole loop.
  3. What was different from the population?  (RR, session, symbol, move size)
  4. What would have improved it?  (concrete and checkable, or "nothing"

WHAT IT WRITES
--------------
  data/trade-lessons.jsonl   append-only, one lesson per resolved trade
  data/trade-lessons.json    rolled-up implications

THE LOOP
--------
Idempotent: a trade already in the lessons file is never re-processed, so it can
run on a cron forever and stays consistent. Each run picks up whatever has
resolved since the last one.

NOTHING HERE TRADES. It reads outcomes and writes notes. It cannot place,
cancel, or size an order, and it changes no strategy parameter.

Read-only on every state file except its own two outputs.
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

AGENT = Path("/opt/trading-agent")
STATE = AGENT / "data/unified-paper.json"
REAL = [AGENT / "data/live-trades.json", AGENT / "data/futures-trades.json"]
LESSONS = AGENT / "data/trade-lessons.jsonl"
ROLLUP = AGENT / "data/trade-lessons.json"

# Paper books this loop learns from. Each entry is (book_name, path). Every lesson
# carries a `book` field and is judged against ITS OWN book's population — the books
# are never pooled, for the same reason paper and real are never pooled: blending two
# systems makes neither auditable.
#
# `ensemble` is the forward paper book of the strategy-generation pipeline
# (ensemble-paper.json). It was OUTSIDE this loop until 2026-10-07 — the generated
# strategies' forward trades were never being lesson-processed even though the
# strategy-generation-loop skill claimed they were. Adding it here is the fix.
PAPER_BOOKS = [
    ("unified", STATE),
    ("ensemble", AGENT / "data/ensemble-paper.json"),
]

# The population threshold found by win_loss_separation.py / rr_circularity_check.py:
# trades at RR>=2 carried all the net; RR<2 lost, in BOTH halves of the sample.
RR_GOOD = 2.0

# Float tolerance for the RR boundary. The dominant design on this system is an
# exact 2:1 (stop 1x, target 2x), and `abs(tp-entry)/abs(entry-sl)` lands a hair
# either side of the literal: 1.3607/1.3879/1.3471 -> 1.9999999999999836 while
# 2421.71/2470.1442/2397.4929 -> 2.0000000000000187. A raw `rr >= RR_GOOD`
# therefore judged the SAME geometry two opposite ways (2026-10-18: 119
# "inside the winning population" vs 56 "below 2.0" on one exact-2.0 cohort).
# 1e-6 is ~5 orders of magnitude above double noise on these price magnitudes and
# far below any real design difference (the next observed designs are 2.5 and 3.0).
RR_TOL = 1e-6


def _rr_meets_good(rr: float | None) -> bool:
    """Is this RR inside the winning population? One comparator, three callers.

    `classify`, `improve` and `why` all ask this question; when each compared the
    raw float separately, a boundary trade got a verdict, an improvement note and
    a 'why' line that contradicted each other. Tolerance-aware so a designed 2:1
    is never split by float division noise.
    """
    return rr is not None and rr >= RR_GOOD - RR_TOL


def _rr_text(rr: float) -> str:
    """Render an RR for a verdict sentence so the number agrees with the verdict.

    `f"RR {rr:.2f}"` rounds 1.996 UP to "2.00", producing the self-contradicting
    line "RR 2.00 below 2.0". When rounding to 2dp would cross the threshold the
    text keeps three decimals instead, so a sub-threshold setup always PRINTS as
    sub-threshold. A boundary (within RR_TOL) setup prints the plain threshold,
    which is what it is by design.
    """
    if abs(rr - RR_GOOD) <= RR_TOL:
        return f"RR {RR_GOOD:.2f}"
    if f"{rr:.2f}" == f"{RR_GOOD:.2f}":
        return f"RR {rr:.3f}"
    return f"RR {rr:.2f}"


def _load(p: Path, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _trade_id(t: dict) -> str:
    """Stable identity for a trade, so the loop never double-processes one."""
    return "|".join(str(t.get(k, "")) for k in ("symbol", "ts_utc", "entry", "size", "pnl"))


# DESIGNED reward:risk per entry function, read off the source constants.
#
# The book stores ONE `sl` per trade and the manage loop OVERWRITES it with the
# trail (unified_trader.py:346-350): >= +1.5% -> sl = entry*1.012, >= +0.5% ->
# sl = entry. So on any trade that ratcheted, the closed record's `sl` is the
# RATCHET, not the risk that was taken. Dividing by it measures survivors:
# `sl` is conditioned on the outcome (the trail only fires after the price rose).
#
# Measured 2026-10-18 on unified-paper.json (3,041 with pnl):
#   sl > entry (trailed)   675   95.3% win   net +70.27
#   sl < entry (intact)  1,557   24.7% win   net -61.11
#   sl == entry (lock)     815   50.4% win   net  +9.32   <- _rr() returned None
# The book nets +18.48, i.e. ALL of it sits in the records whose geometry was
# rewritten, and 27% of the book was being written as UNKNOWN by the zero-risk
# denominator of the breakeven lock.
#
# Each entry function hardcodes a target multiplier on a fixed stop, so the RR it
# designs is a CONSTANT regardless of price:
#   _paper_open            stop=s,   tp=c(1+2.0s)  -> 2.0
#   _pullback_open         stop=.012 tp=c(1+1.5s)  -> 1.5
#   _paper_loose_open      stop=.012 tp=c(1+1.5s)  -> 1.5
#   _imported_strategy_open stop=.02 tp=c(1+1.8s)  -> 1.8
# Validated against the 1,551 records whose sl was never touched: 93.9% agree to
# within 0.02 RR; the 6% residual is 4dp price rounding (2.04 vs 2.0).
DESIGNED_RR_BY_LANE = {"loose": 1.5, "imported": 1.8, "paper": 2.0}


def _designed_rr(t: dict) -> float | None:
    """The RR the entry function DESIGNED, independent of any trail rewrite.

    Returns None only when the geometry is genuinely unrecorded — not when the
    stored stop happens to sit at or above the entry, which is the ratchet.
    """
    try:
        e, tp, sl = float(t["entry"]), float(t["tp"]), float(t["sl"])
    except Exception:
        return None
    if e <= 0:
        return None
    trailed = sl >= e
    if not trailed:
        risk = e - sl
        if risk <= 0:
            return None
        return abs(tp - e) / risk
    # The stored stop is a ratchet level. Use the lane's designed multiplier.
    if t.get("loose"):
        return DESIGNED_RR_BY_LANE["loose"]
    if t.get("imported"):
        return DESIGNED_RR_BY_LANE["imported"]
    return DESIGNED_RR_BY_LANE["paper"]


def _rr_stored(t: dict) -> float | None:
    """The naive RR from the stored geometry — kept for comparison, never for a verdict."""
    try:
        e, tp, sl = float(t["entry"]), float(t["tp"]), float(t["sl"])
    except Exception:
        return None
    risk = abs(e - sl)
    if risk <= 0:
        return None
    return abs(tp - e) / risk


def geometry(t: dict) -> dict:
    """Both numbers, so a reader can see when the trail and the design disagree."""
    e = t.get("entry")
    sl = t.get("sl")
    try:
        trailed = sl is not None and e is not None and float(sl) >= float(e)
    except Exception:
        trailed = False
    return {
        "rr": _designed_rr(t),
        "rr_stored": _rr_stored(t),
        "sl_trailed": trailed,
    }


def _rr(t: dict) -> float | None:
    """RR of the setup AS DESIGNED at entry.

    ⚠️ Do NOT go back to `abs(tp-entry)/abs(entry-sl)` on the closed record: the
    trail overwrites `sl`, so that denominator is the ratchet. See
    `_designed_rr` and `tests/test_lesson_trailed_sl.py`.
    """
    return _designed_rr(t)


def _session(ts: str) -> str:
    try:
        h = datetime.fromisoformat(ts.replace("Z", "+00:00")).hour
    except Exception:
        return "UNKNOWN"
    if 0 <= h < 8:
        return "Asia"
    if 8 <= h < 16:
        return "London"
    return "NY"


def _drop_torn_tail(f) -> None:
    """Truncate an appended ledger to its last COMPLETE line, in place.

    Called with the append handle still open. If the tail does not parse as
    JSON, seek back to just after the last newline that closes a parseable
    line and truncate there; everything before it is untouched.
    """
    f.flush()
    size = f.tell()
    if size == 0:
        return
    with open(LESSONS, "rb") as r:
        data = r.read()
    if not data:
        return
    if data.endswith(b"\n"):
        # A complete trailing line still has to PARSE. The earlier version
        # returned here on the newline alone, which is the common torn case:
        # a kill between the json body and its "\n".
        cut = data[:-1].rfind(b"\n")
        try:
            json.loads(data[cut + 1:])
            return
        except Exception:
            with open(LESSONS, "wb") as w:
                w.write(data[:cut + 1] if cut >= 0 else b"")
            return
    cut = data.rfind(b"\n")
    keep = data[:cut + 1] if cut >= 0 else b""
    with open(LESSONS, "wb") as w:
        w.write(keep)


def _seen() -> set[str]:
    out: set[str] = set()
    try:
        with open(LESSONS, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    out.add(json.loads(line).get("id", ""))
    except Exception:
        pass
    return out


def population(closed: list[dict]) -> dict:
    """What is normal for this book, so a single trade can be compared to it."""
    p = [float(t.get("pnl") or 0) for t in closed]
    wins = [x for x in p if x > 0]
    losses = [x for x in p if x <= 0]
    rrs = [r for r in (_rr(t) for t in closed) if r is not None]
    iv = instrument_validity(closed)
    return {
        "n": len(p),
        "win_rate": (len(wins) / len(p) * 100) if p else 0.0,
        "avg_win": statistics.mean(wins) if wins else 0.0,
        "avg_loss": statistics.mean(losses) if losses else 0.0,
        "median_rr": statistics.median(rrs) if rrs else None,
        "instrument_invalid": iv["instrument_invalid"],
        "instrument_evidence": iv,
    }


def instrument_validity(closed: list[dict]) -> dict:
    """Could this book have recorded the outcome it is being judged on?

    A forward paper book whose target was unreachable by construction measures
    hold-time noise, not the strategy — and its failure shape looks EXACTLY like a
    strategy result (a wall of TIME exits, a low win rate). Judging its trades on
    RR/PnL produces confident verdicts about a rule the instrument never tested.

    The test is deliberately population-level and recorded-field-only:
      * every trade carries TP/SL geometry, and
      * ZERO trades exited at TP, and
      * the target was identical across the book (a per-member hardcoded stop
        derives an identical TP, so the geometry is a CONSTANT, not a choice).

    If all three hold, the target was unreachable for the whole population and no
    per-trade verdict about this book is supported. Returns the evidence either way.

    ACTIONABILITY (added 2026-10-16). "0 TP exits" states that the target was
    never reached; it does not say how FAR the instrument actually moved, so the
    reader cannot tell a target that is 0.2% too far from one that is 40x too far,
    nor what geometry WOULD have been reachable. Both `ensemble-paper.json` cases
    on this system have been the second kind (target +3.00% against a maximum
    realised move of +2.27%), where the correct fix is the exit horizon or the
    target, not the stop. So when the target is invalid we also report the
    realised-move distribution actually observed — min / median / p90 / max, in
    percent, from `entry` to `exit` on the records that carry both. That is the
    number the target has to clear, and it is recorded-field-only, so it cannot
    invent evidence the book did not produce. Best-move (`best_px`) is reported
    separately when present: it is the furthest INTRA-TRADE excursion and shows
    whether the target was ever even touched, not just where the trade ended.
    """
    geo = []
    tps = []
    moves = []
    bests = []
    for t in closed:
        r = _rr(t)
        if r is None:
            continue
        geo.append(t)
        try:
            e, tp = float(t["entry"]), float(t["tp"])
            if e:
                tps.append(round((tp - e) / e * 100, 3))
        except Exception:
            pass
        # Realised exit move — what the trade actually did, entry -> exit.
        try:
            e, x = float(t["entry"]), float(t["exit"])
            if e:
                moves.append((x - e) / e * 100)
        except Exception:
            pass
        # Best intra-trade excursion — how close the target ever got.
        try:
            e, bp = float(t["entry"]), float(t["best_px"])
            if e:
                bests.append((bp - e) / e * 100)
        except Exception:
            pass
    tp_exits = sum(1 for t in closed if str(t.get("reason") or "") == "TP")
    n = len(closed)
    identical_tp = len(tps) >= 10 and len(set(tps)) == 1
    invalid = bool(geo) and tp_exits == 0 and identical_tp

    def _pct(nums: list[float], q: float) -> float | None:
        """Nearest-rank percentile of a list, or None when empty."""
        if not nums:
            return None
        s = sorted(nums)
        idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
        return round(s[idx], 3)

    move_dist = None
    if moves:
        move_dist = {
            "n": len(moves),
            "min": _pct(moves, 0.0),
            "median": _pct(moves, 0.5),
            "p90": _pct(moves, 0.9),
            "max": _pct(moves, 1.0),
        }
    out = {
        "n": n,
        "with_geometry": len(geo),
        "tp_exits": tp_exits,
        "distinct_tp_pct": sorted(set(tps)),
        "identical_target": identical_tp,
        "realised_move_pct": move_dist,
        "max_best_move_pct": round(max(bests), 3) if bests else None,
        "instrument_invalid": invalid,
    }
    # Only claim reachability when we have BOTH the target and the observed move.
    if invalid and move_dist and move_dist["max"] is not None:
        out["target_exceeds_max_move"] = (
            max(tps) > move_dist["max"] if tps else None)
    return out


def threshold_validity(lessons: list[dict]) -> dict:
    """Does RR_GOOD actually sit where the population changes sign?

    RR_GOOD was derived from `win_loss_separation.py` / `rr_circularity_check.py`
    when "trades at RR>=2 carried all the net" was true of the book. That was a
    measured fact about a SAMPLE, not a law. Re-derived on the 2026-10-18 lesson
    ledger it no longer holds: RR>=2 is net-POSITIVE only above ~2.1, while the
    whole 2.0-2.1 band is net-NEGATIVE (-11.59 over 240 trades). A threshold
    quoted at a level where the population does not change sign will label ~240
    losing trades "inside the winning population" and tell the owner their losses
    were "variance, not design".

    This does not move the threshold — that is a strategy decision, not a
    reporting one. It REPORTS the net on each side of the boundary and flags when
    the sign change is not where the constant claims, so the next reader inherits
    the discrepancy instead of re-discovering it.
    """
    rows = [l for l in lessons
            if l.get("rr") is not None and l.get("verdict") not in ("UNKNOWN", "GEOMETRY-INVALID")]
    if not rows:
        return {"n": 0, "ok": None, "note": "no RR-classified lessons"}
    good = [l for l in rows if _rr_meets_good(l["rr"])]
    bad = [l for l in rows if not _rr_meets_good(l["rr"])]

    def _agg(g):
        return {"n": len(g),
                "net": round(sum(float(l.get("pnl") or 0) for l in g), 3),
                "wr": round(100 * sum(1 for l in g if float(l.get("pnl") or 0) > 0) / len(g), 1)
                if g else None}

    # Scan upward for the lowest RR band (0.05 wide) that is net-positive, so the
    # report can name where the edge actually starts rather than only that it is
    # not where we said.
    first_paying = None
    lo = RR_GOOD
    for _ in range(20):
        band = [l for l in rows if lo <= l["rr"] < lo + 0.05]
        if len(band) >= 20 and sum(float(l.get("pnl") or 0) for l in band) > 0:
            first_paying = round(lo, 2)
            break
        lo = round(lo + 0.05, 2)
    out = {
        "threshold": RR_GOOD,
        "good_side": _agg(good),
        "bad_side": _agg(bad),
        "first_paying_band": first_paying,
        # The coarse test (signs differ across the boundary) is NOT enough: it
        # passed on this book while the 2.00-2.05 band lost -10.53 over 228
        # trades and the first paying band was 2.40. The claim the constant makes
        # is "at/above the threshold pays", so the check is whether the threshold
        # IS the first paying band, not merely that the two sides differ overall.
        "coarse_signs_differ": bool(good and bad and _agg(good)["net"] > 0 > _agg(bad)["net"]),
        "ok": bool(first_paying is not None and first_paying <= RR_GOOD + 0.05),
    }
    if not out["ok"]:
        lag = (round(first_paying - RR_GOOD, 2) if first_paying is not None else None)
        out["note"] = (
            f"RR_GOOD={RR_GOOD} is NOT where this book starts paying: the first "
            f"net-paying band (n>=20) is {first_paying}"
            + (f" — {lag} above the constant" if lag else "")
            + f". The >= side nets {out['good_side']['net']} only because of trades "
            f"above that band; the threshold band itself is net-negative. Verdicts "
            "quoting the constant as 'the winning population' are mislabelling the "
            "trades in between.")
    return out


def rr_split(lessons: list[dict]) -> dict | None:
    """Do the first and second half of the ledger tell DIFFERENT stories about RR<2?

    Born 2026-10-19. Commit 0c00a42 correctly retracted the "RR<2 loses, skip it"
    advice, and supported the retraction with two numbers:

        "it is +9.95 in the first half and -1.20 in the second"

    Those were a fact about a 3,171-lesson ledger on 2026-10-18. They were then
    written into the source as a STRING, and one day / 24 lessons later the same
    computation returns +11.17 / +0.04 — the second half is net-POSITIVE. The
    conclusion survives (RR does not separate the book: the around-2.0 band still
    does not clear the fee, and the split is not a stable feature) but the
    evidence printed beside it had already expired. Quoting a stale measurement
    as current is the Rule-7 failure in prose form.

    So the split is DERIVED on every run from the same ledger
    `threshold_validity` reads, and only reported when it is reproduced under
    both orderings. Order matters here more than it looks: most exact-2.0 fills
    arrive inside the same second (two timeframe lanes firing together), so a
    cut that lands between them drops up to 4 near-identical fills from the
    first half — moving the second-half net by ~$18 across 15 monthly cut
    points. A split that cannot be cut cleanly is not reported at all.

    """
    rows = [r for r in lessons
            if r.get("rr") is not None and r.get("verdict") not in ("UNKNOWN", "GEOMETRY-INVALID")]
    if len(rows) < 200:
        return None

    def _halves(seq):
        k = len(seq) // 2
        return _dict_halves(seq[:k], seq[k:])

    # The cut POINT and the cut RESULT are two different questions, and conflating
    # them made the guard unreliable in the direction that loses information.
    #
    #   * Where to cut: the median TRADE TIME. "First half" has to mean the earlier
    #     half of the book, otherwise the sentence "pays early, decays later" is
    #     about file order, not about time — and file order here is settlement order,
    #     which lags trade time by whole hours.
    #   * Is the cut clean: does it fall inside an instant-bucket? Most exact-2.0
    #     fills arrive in the same second (two timeframe lanes firing together), so
    #     such a cut drops up to 4 near-identical fills from the first half and moves
    #     the second-half net by ~$18.
    #
    # The old version answered both with time order and then required the INSTANT to
    # be identical on both sides of the median — which is not what "the cut falls
    # inside an instant-bucket" means, and refused every ledger whose two middle
    # trades were merely close (measured 2026-10-19: median cut at 12:46 vs 13:16,
    # half an hour apart, reported as "inside a cluster"). Ask the question directly:
    # is the fill AT the median index in the same instant as any OTHER fill nearby?
    by_time = sorted(rows, key=lambda r: str(r.get("trade_ts_utc") or ""))
    k = len(rows) // 2
    cut_ts = str(by_time[k].get("trade_ts_utc") or "")
    # Same-instant neighbours ACROSS the cut are what makes the split arbitrary.
    # A cluster that lies entirely on one side is fine — it stays in one half.
    left_same = sum(1 for r in by_time[:k]
                    if str(r.get("trade_ts_utc") or "") == cut_ts)
    right_same = sum(1 for r in by_time[k:]
                     if str(r.get("trade_ts_utc") or "") == cut_ts)
    if left_same and right_same:
        return None  # the cut would split one instant: not a time split
    if not cut_ts:
        return None  # no trade time recorded: no time split exists

    a, b = _halves(by_time), _halves(rows)
    # File order is the SECOND reader: it must reproduce the same halves. On this
    # ledger it does not — settlement order lags trade time, so the two orderings
    # disagree around the cut (measured: 4 fills swap sides, +11.45/-0.24 by time
    # vs +11.74/-0.53 by file). That disagreement is the fragility the guard was
    # built to catch, so it is reported as no-claim rather than papered over with
    # a `cut_ts` from one ordering beside a net from the other.
    if (a["first_n"], a["second_n"]) != (b["first_n"], b["second_n"]):
        return None
    if (a["first_net"], a["second_net"]) != (b["first_net"], b["second_net"]):
        return None
    # `order_independent` claims exactly what was verified above: the cut falls
    # between two fills a full timestamp apart, so re-ordering the ledger cannot
    # move a different set of fills across it. It does NOT mean file order and
    # time order agree — they do not, on this ledger (settlement order lags trade
    # time, 2,150 of 3,090 positions differ). The cut is by TIME; file order is
    # only used as the second reader that must reproduce the same halves.
    return {**a, "n": len(rows), "cut_ts": cut_ts, "order_independent": True}


def _dict_halves(first: list[dict], second: list[dict]) -> dict:
    """RR<2 net/loss counts for each half, and the full halves' net for context."""
    def _bad(g):
        return [r for r in g if not _rr_meets_good(r["rr"])]

    fb, sb = _bad(first), _bad(second)
    return {
        "first_n": len(fb),
        "second_n": len(sb),
        "first_net": round(sum(float(r.get("pnl") or 0) for r in fb), 3),
        "second_net": round(sum(float(r.get("pnl") or 0) for r in sb), 3),
        "first_all_net": round(sum(float(r.get("pnl") or 0) for r in first), 3),
        "second_all_net": round(sum(float(r.get("pnl") or 0) for r in second), 3),
    }


def rr_split_text(split: dict | None) -> str:
    """One sentence about RR<2's halves — and it must not overclaim a decay.

    The shape that justified "RR does not separate this book" was first half
    POSITIVE, second half NEGATIVE. When the derived halves no longer have that
    shape the sentence says the shape did not hold, instead of printing two
    numbers and letting the reader assume it did.
    """
    if not split:
        return ("no half-split is quotable on this ledger (a clean cut would fall "
                "inside a cluster of simultaneous fills), so no first/second-half "
                "claim is made either way")
    f, s = split["first_net"], split["second_net"]
    base = (f"the RR<{RR_GOOD} population is {f:+.2f} in the first half (n="
            f"{split['first_n']}) and {s:+.2f} in the second (n={split['second_n']})")
    if f > 0 > s:
        return (f"{base} — the 'pays early, decays later' shape is still present, but "
                "a half-split is not a stable edge")
    return (f"{base} — the first-half-pays / second-half-loses shape did NOT hold on "
            "this run, so the earlier decay claim is stale")


def classify(t: dict, pop: dict) -> tuple[str, str]:
    """Separate a GOOD DECISION from a GOOD OUTCOME.

    The logic:
      RR >= 2        -> the setup had positive expectancy as designed
      RR <  2        -> the setup was one of the losing population, by design

    A win at low RR  = GOOD OUTCOME, POOR DECISION  (the design was unfavourable)
    A loss at high RR = GOOD DECISION, POOR OUTCOME (bad luck inside a good setup)
    A win at high RR  = both aligned
    A loss at low RR  = the design said so; not a surprise and not a lesson

    If the BOOK itself is instrument-invalid (target unreachable for the whole
    population — see instrument_validity), the per-trade RR/PnL verdict is not
    supported by the instrument and is reported as GEOMETRY-INVALID instead. This
    is the "if no trade ever reached its target, the TARGET was the problem"
    guard, enforced in code rather than only in prose.
    """
    pnl = float(t.get("pnl") or 0)
    rr = _rr(t)
    if rr is None:
        return ("UNKNOWN", "no TP/SL geometry recorded, so the setup cannot be judged")
    if pop.get("instrument_invalid"):
        ev = pop.get("instrument_evidence", {})
        return ("GEOMETRY-INVALID",
                "book-level: target was unreachable for the whole population "
                f"(0 TP exits over {ev.get('n')}, identical target "
                f"{ev.get('distinct_tp_pct')}%) — this trade cannot speak about the rule")
    good_setup = _rr_meets_good(rr)
    won = pnl > 0
    if good_setup and won:
        return ("ALIGNED", "good setup, good outcome")
    if good_setup and not won:
        return ("GOOD DECISION, POOR OUTCOME",
                f"{_rr_text(rr)} is inside the winning population; the loss is variance, not design")
    if not good_setup and won:
        return ("GOOD OUTCOME, POOR DECISION",
                f"{_rr_text(rr)} is inside the losing population; this win is luck, not repeatable")
    return ("AS DESIGNED", f"{_rr_text(rr)} below {RR_GOOD}; this population loses on average")


def why(t: dict, pop: dict) -> list[str]:
    """Question 1 and 3: what was true, and how it differs from normal."""
    out: list[str] = []
    reason = str(t.get("reason") or "?")
    rr = _rr(t)
    out.append(f"exit={reason}")
    if reason == "TP":
        out.append("target reached — the thesis played out")
    elif reason == "SL":
        out.append("stop hit — the thesis failed or the move went against first")
    elif reason == "TIME":
        out.append("closed on time — neither level reached; the setup neither worked nor failed")
    elif reason == "STALL":
        out.append("closed on stall — momentum faded before either level")
    if rr is not None:
        rel = "at/above" if _rr_meets_good(rr) else "below"
        out.append(f"{_rr_text(rr)} ({rel} the {RR_GOOD} threshold)")
    sess = _session(str(t.get("ts_utc", "")))
    if sess != "UNKNOWN":
        out.append(f"session {sess}")
    return out


_SPLIT_CACHE: dict = {"key": None, "val": None}


def _current_split() -> dict | None:
    """rr_split() of the ledger as it is on disk, memoised per (size, mtime)."""
    try:
        st = LESSONS.stat()
        key = (st.st_size, st.st_mtime_ns)
    except Exception:
        return None
    if _SPLIT_CACHE["key"] != key:
        try:
            rows = [json.loads(l) for l in open(LESSONS, encoding="utf-8") if l.strip()]
        except Exception:
            return None
        _SPLIT_CACHE["val"] = rr_split(rows)
        _SPLIT_CACHE["key"] = key
    return _SPLIT_CACHE["val"]


_SENTINEL = object()


def improve(t: dict, verdict: str, pop: dict, split=_SENTINEL) -> list[str]:
    """Question 4: what would have improved it — concrete, or honestly 'nothing'.

    `split` is the DERIVED RR<2 half-split (see `rr_split`). Passing None means
    "there is no quotable split, say so"; OMITTING it reads the ledger on disk.
    A plain `= None` default would have made the two indistinguishable and
    silently shipped the fallback sentence on every live lesson.
    """
    if split is _SENTINEL:
        split = _current_split()
    rr = _rr(t)
    pnl = float(t.get("pnl") or 0)
    why_ = str(t.get("reason") or "")
    out: list[str] = []

    if rr is not None and not _rr_meets_good(rr):
        # NOT "the measured population that loses" any more. On DESIGNED geometry
        # (see test_lesson_trailed_sl.py) the RR<2 population is net-positive
        # overall, so "skipping it would have avoided this trade" is not
        # supported. The supporting halves are MEASURED, not remembered: the
        # +9.95/-1.20 pair written here on 2026-10-18 was stale within a day
        # (see test_lesson_split_freshness.py).
        out.append(f"setup was below RR {RR_GOOD} — RR does not separate this book: "
                   f"{rr_split_text(split)}. This is not a reason to skip the setup "
                   "or to promote it")
    if why_ == "TIME":
        out.append("exited on time without reaching either level — a looser or "
                   "adaptive hold, or not entering, both deserve a paper test")
    if why_ == "SL" and _rr_meets_good(rr):
        out.append("good setup stopped out — do NOT loosen the stop on this evidence; "
                   "one instance is variance, not a parameter signal")
    if verdict == "GOOD OUTCOME, POOR DECISION":
        out.append("the win is not evidence this setup works — it should not be counted "
                   "toward promoting the setup")
    if not out:
        out.append("nothing identified — the trade behaved as its geometry implied")
    return out


def write_rollup() -> None:
    """Roll up the lessons file into data/trade-lessons.json.

    Runs on EVERY invocation, including runs that wrote no new lessons — the
    instrument-validity warning must be visible on a quiet run too, otherwise a
    reader who happens to look at a no-new-trades hour sees no sign that a book
    is measuring an unreachable target.
    """
    try:
        all_lessons = []
        with open(LESSONS, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    all_lessons.append(json.loads(line))
        # Verdicts and populations are reported PER BOOK — never pooled, so a
        # reader can always tell which book a number came from. Legacy lessons
        # written before the `book` field existed default to "unified".
        vc_by_book: dict[str, Counter] = defaultdict(Counter)
        for l in all_lessons:
            vc_by_book[l.get("book", "unified")][l["verdict"]] += 1
        pop_by_book: dict[str, dict] = {}
        for book, path in PAPER_BOOKS:
            st_b = _load(path, {}) or {}
            pop_by_book[book] = population(list(st_b.get("closed") or []))
        rolls = {
            "as_of_utc": datetime.now(timezone.utc).isoformat(),
            "lessons_total": len(all_lessons),
            "verdicts_by_book": {b: dict(c) for b, c in vc_by_book.items()},
            "verdicts": dict(sum(vc_by_book.values(), Counter())),
            "population_by_book": pop_by_book,
            "rr_threshold": RR_GOOD,
            "implications": [],
        }
        # Surface any book whose geometry made its outcomes predetermined. This is
        # the loud version of the "if no trade reached its target, the TARGET was
        # the problem" rule: a reader must not be able to read the ensemble stats
        # without seeing that the instrument is invalid.
        rolls["instrument_warnings"] = [
            {"book": b, **pop_by_book[b]["instrument_evidence"]}
            for b in pop_by_book
            if pop_by_book[b].get("instrument_invalid")
        ]
        gd = sum(c.get("GOOD DECISION, POOR OUTCOME", 0) for c in vc_by_book.values())
        go = sum(c.get("GOOD OUTCOME, POOR DECISION", 0) for c in vc_by_book.values())
        for w in rolls["instrument_warnings"]:
            md = w.get("realised_move_pct") or {}
            # Make the warning ACTIONABLE: state the target, the best move the
            # instrument ever produced, and the shape of the distribution, so the
            # reader can see whether the target is slightly or wildly too far —
            # and what would have been reachable. Falls back to the bare sentence
            # when the book records no entry/exit (recorded-field-only).
            if md.get("max") is not None:
                rolls["implications"].insert(0,
                    f"INSTRUMENT INVALID [{w['book']}]: {w['n']} closed, 0 TP "
                    f"exits, target {w['distinct_tp_pct']}% — but the realised "
                    f"move was min {md['min']}% / median {md['median']}% / p90 "
                    f"{md['p90']}% / max {md['max']}% (n={md['n']}). The target "
                    f"is {'' if w.get('target_exceeds_max_move') else 'NOT '}above "
                    "the best move the book ever produced, so it was unreachable "
                    "by construction; no per-trade verdict about this book's "
                    "strategy is supported. Fix the TARGET or the HOLD HORIZON "
                    "(the realised max is the ceiling a reachable target must "
                    "clear), not the stop.")
            else:
                rolls["implications"].insert(0,
                    f"INSTRUMENT INVALID [{w['book']}]: {w['n']} closed, 0 TP exits, "
                    f"identical target {w['distinct_tp_pct']}% — the target was "
                    "unreachable by construction; no per-trade verdict about this "
                    "book's strategy is supported. Fix the geometry before reading "
                    "any of its stats.")
        if go:
            rolls["implications"].append(
                f"{go} wins came from setups the population LOSES on — "
                "these must not count toward promoting a strategy")
        if gd:
            rolls["implications"].append(
                f"{gd} losses came from setups the population WINS on — "
                "do not loosen stops or abandon the setup on this evidence")
        # Is the RR threshold itself still where the population changes sign?
        tv = threshold_validity(all_lessons)
        rolls["threshold_validity"] = tv
        if tv.get("ok") is False:
            rolls["implications"].append(
                f"RR THRESHOLD STALE: {tv['note']}")
        # The RR<2 half-split is measured on THIS run's ledger, never quoted from
        # memory: one derived here also feeds improve() below.
        split = rr_split(all_lessons)
        rolls["rr_split"] = split
        # The skip-advice has to be TRUE of the book it is printed about. It was
        # hardcoded on the belief that RR<2 always loses; once RR is measured on
        # DESIGNED geometry (2026-10-18) that stopped being so, and a rollup that
        # still prints "skipping them is the largest improvement available" over a
        # net-POSITIVE population is advice to delete the profitable cohort.
        bad = tv.get("bad_side") or {}
        if bad.get("n"):
            if bad.get("net", 0) < 0:
                rolls["implications"].append(
                    f"The RR<{RR_GOOD} population is net-negative ({bad['net']:+.2f} "
                    f"over {bad['n']}); skipping those setups is the single largest "
                    "measured improvement available")
            else:
                rolls["implications"].append(
                    f"The RR<{RR_GOOD} population is NOT net-negative ({bad['net']:+.2f} "
                    f"over {bad['n']}, WR {bad.get('wr')}%) once RR is measured on "
                    "DESIGNED geometry — the 'RR<2 loses, skip it' finding was an "
                    "artefact of the trail overwriting `sl`. But it is NOT a buy "
                    f"signal either: {rr_split_text(split)}. Do not cut on RR, and "
                    "do not promote on it. See test_lesson_trailed_sl.py and "
                    "test_lesson_split_freshness.py")
        with open(ROLLUP, "w", encoding="utf-8") as f:
            json.dump(rolls, f, indent=2)
    except Exception as e:
        print(f"rollup failed: {e}")


def repair_lessons(dry_run: bool = True) -> dict:
    """Recompute `rr`/`verdict`/`why`/`what_would_improve` on ALREADY-WRITTEN lessons.

    The lessons file is append-only and idempotent, so a fix to `_rr()` only ever
    reaches NEW trades. The 3,171 lessons written before 2026-10-18 carry the RR
    that was computed from the trail-mutated `sl` — and `threshold_validity()`
    reads `rr` straight off those records, so a stale ledger keeps reporting a
    stale threshold verdict however correct the code becomes.

    Identity is untouched: `id`, `ts_utc`, `book`, `symbol`, `trade_ts_utc`,
    `pnl`, `exit_reason` are never rewritten. Only the DERIVED fields are. The
    trade record behind each lesson is looked up in its own book so the raw
    geometry (loose/imported flags) is available; a lesson whose trade cannot be
    found is left alone and counted.

    Default is dry_run: it reports what it WOULD change and writes nothing.
    """
    try:
        rows = [json.loads(l) for l in open(LESSONS, encoding="utf-8") if l.strip()]
    except Exception as e:
        return {"error": str(e)}

    books = {name: _load(path, {}) or {} for name, path in PAPER_BOOKS}
    pops = {name: population(list(st.get("closed") or [])) for name, st in books.items()}
    lookup = {}
    for name, st in books.items():
        for t in (st.get("closed") or []):
            lookup[(name, _trade_id(t))] = t

    out = rows[:]
    changed = missing = 0
    flips: Counter = Counter()
    split = rr_split(rows)
    for i, l in enumerate(rows):
        book = l.get("book", "unified")
        t = lookup.get((book, l.get("id")))
        if t is None:
            missing += 1
            continue
        pop = pops.get(book) or {"instrument_invalid": False, "instrument_evidence": {}}
        verdict, note = classify(t, pop)
        g = geometry(t)
        new = dict(l)
        new["rr"] = g["rr"]
        new["verdict"] = verdict
        new["verdict_note"] = note
        new["why"] = why(t, pop)
        new["what_would_improve"] = improve(t, verdict, pop, split=split)
        if g["sl_trailed"]:
            new["rr_stored"] = g["rr_stored"]
            new["sl_trailed"] = True
        if new != l:
            changed += 1
            if l.get("verdict") != verdict:
                flips[f"{l.get('verdict')} -> {verdict}"] += 1
            out[i] = new

    result = {"lessons": len(rows), "changed": changed, "unmatched": missing,
              "verdict_flips": dict(flips.most_common()), "dry_run": dry_run}
    if not dry_run and changed:
        tmp = LESSONS.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for l in out:
                f.write(json.dumps(l) + "\n")
        tmp.replace(LESSONS)
        write_rollup()
    return result


def main() -> int:
    seen = _seen()
    tallies: Counter = Counter()
    written = 0
    per_book_new: dict[str, int] = {}
    # Every lesson written by this run quotes the RR<2 half-split, so it must be
    # computed ONCE here, at write time, from the ledger it describes (see
    # `rr_split`). It used to be referenced as a bare `split` inside the write
    # loop below, where nothing defined it: the NameError was raised INSIDE the
    # `with open(LESSONS, "a")` block, so the file was already truncated to its
    # prior length and every trade in `work` went unlearned, every hour, while
    # stdout still printed "new from unified: N".
    split = _current_split()

    # Collect new trades across every paper book, judged against its own population.
    # The books are processed separately so a trade is never classified against a
    # foreign population.
    work: list[tuple[str, dict, list[dict], dict]] = []
    for book, path in PAPER_BOOKS:
        st = _load(path, {}) or {}
        closed = list(st.get("closed") or [])
        if not closed:
            per_book_new[book] = 0
            continue
        pop = population(closed)
        new = [t for t in closed if _trade_id(t) not in seen]
        per_book_new[book] = len(new)
        if new:
            work.append((book, pop, new, st))

    print(f"lessons already written : {len(seen)}")
    for book, path in PAPER_BOOKS:
        print(f"  new from {book:<9}: {per_book_new.get(book, 0)}")
    print()

    if not any(per_book_new.values()):
        print("nothing new — loop is up to date")
        write_rollup()
        return 0

    try:
        with open(LESSONS, "a", encoding="utf-8") as f:
            for book, pop, new, _st in work:
                for t in new:
                    verdict, note = classify(t, pop)
                    tallies[(book, verdict)] += 1
                    lesson = {
                        "v": 1,
                        "ts_utc": datetime.now(timezone.utc).isoformat(),
                        "id": _trade_id(t),
                        "book": book,
                        "symbol": t.get("symbol"),
                        "trade_ts_utc": t.get("ts_utc"),
                        "pnl": float(t.get("pnl") or 0),
                        "rr": _rr(t),
                        "session": _session(str(t.get("ts_utc", ""))),
                        "exit_reason": t.get("reason"),
                        "verdict": verdict,
                        "verdict_note": note,
                        "why": why(t, pop),
                            "what_would_improve": improve(t, verdict, pop, split=split),
                    }
                    f.write(json.dumps(lesson) + "\n")
                    written += 1
            # Truncate to the last complete line before leaving. A crash or
            # a kill mid-write leaves a torn JSON line that `_seen()` cannot
            # parse, and every later run then dies on the same line. Only
            # reachable if a write partially flushed; normal runs are no-ops.
            f.flush()
            _drop_torn_tail(f)
    except Exception as e:
        # A partial run must still leave the loop in a state it can resume from:
        # say how many of the `new` trades were actually written, so "written"
        # and the per-book counts agree with the file on disk.
        print(f"could not write lessons after {written}: {type(e).__name__}: {e}")
        return 1

    # rollup
    write_rollup()

    print(f"lessons written this run : {written}")
    for (book, verdict), v in tallies.most_common():
        print(f"  [{book:<8}] {verdict:<32} {v}")
    print()
    # Loud instrument check — a book whose target was unreachable cannot be read
    # for strategy signal, and that must be visible on the cron's own output.
    for book, path in PAPER_BOOKS:
        st_b = _load(path, {}) or {}
        iv = instrument_validity(list(st_b.get("closed") or []))
        if iv["instrument_invalid"]:
            md = iv.get("realised_move_pct") or {}
            print(f"  !! INSTRUMENT INVALID [{book}]: {iv['n']} closed, "
                  f"0 TP exits, identical target {iv['distinct_tp_pct']}%")
            if md.get("max") is not None:
                print(f"     realised move: min {md['min']}% / median "
                      f"{md['median']}% / p90 {md['p90']}% / max {md['max']}% "
                      f"(n={md['n']}); best intra-trade +{iv.get('max_best_move_pct')}%")
            print(f"     target unreachable by construction — its verdicts are not "
                  f"evidence about the strategy")
    print()
    print("GOOD DECISION vs GOOD OUTCOME is the point of this loop:")
    print("  a win on a bad setup is luck and must not be learned from;")
    print("  a loss on a good setup is variance and must not be tuned away.")
    return 0


if __name__ == "__main__":
    # The hourly cron runs with no user present. It must be SILENT when there is
    # nothing new, because empty stdout is what the scheduler treats as "nothing
    # to report" — a job that posts "nothing new" every hour trains the reader
    # to ignore it. Detail is always written to the two data files regardless.
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing unless new lessons were written")
    ap.add_argument("--repair", action="store_true",
                    help="recompute derived fields on existing lessons (writes)")
    ap.add_argument("--repair-dry-run", action="store_true",
                    help="report what --repair would change; writes nothing")
    a = ap.parse_args()

    if a.repair or a.repair_dry_run:
        res = repair_lessons(dry_run=not a.repair)
        print(json.dumps(res, indent=2))
        sys.exit(0 if "error" not in res else 1)

    if a.quiet:
        import io

        buf = io.StringIO()
        _real = sys.stdout
        sys.stdout = buf
        rc = main()
        sys.stdout = _real
        text = buf.getvalue()
        # Silent only when genuinely nothing new. The output shape changed when
        # ensemble-paper.json was added as a second book (2026-10-07), so key on
        # the stable phrase rather than the old per-line counter format.
        if "nothing new" not in text:
            print(text.rstrip())
        sys.exit(rc)

    sys.exit(main())
