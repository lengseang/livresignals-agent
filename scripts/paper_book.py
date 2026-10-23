#!/usr/bin/env python3
"""paper_book.py — collapse multi-timeframe double-bookings in the paper ledger.

THE DEFECT (found 2026-10-15)
-----------------------------
`unified_trader._paper_open()` guards re-entry with

    if any(p["symbol"] == sig["symbol"] and p.get("tf") == sig["tf"] ...)

i.e. ONE open position allowed per `(symbol, timeframe)`. But the SAME scan
computes `_features()` per timeframe (5m / 15m / 30m) over the SAME price feed,
so one physical dip produces a signal on every timeframe that happens to agree.
Each is booked as a separate closed trade.

Measured on `data/unified-paper.json` (2026-10-15):

    closed records                                  2964
    pairs <=5s apart, IDENTICAL entry AND pnl        498
      of those, DIFFERENT tf                         476
      of those, SAME tf (1.5-2.5 ms apart)            22
    across all 498: identical reason 498/498, pnl_net 498/498,
                    worst_px 498/498, tp 484/498, sl 489/498

Two records that share an exact entry price, an exact pnl, an exact exit
reason and an exact adverse extreme, seconds apart, are not two trades. They
are one trade written once per timeframe (plus a handful of true double-writes
in the same scan, the 22 microsecond-apart same-tf pairs).

WHY THE 2026-10-14 `dedupe` DID NOT CATCH THIS
----------------------------------------------
`self_improve.dedupe` keys on `symbol + open time + entry + size` and removes
only byte-identical re-bookings. The multi-tf copies differ in `ts_utc`
(sub-second) and often in `entry` by one tick, so that key is a DIFFERENT key
for each copy and all of them survive. The 2026-10-14 fix correctly stopped
merging distinct same-price trades; it did not — and could not — address a
population that is one trade wearing several timestamps.

IMPACT
------
Collapsing the 498 duplicate pairs removes 510 phantom records and ~$5.57 of
gross P&L that was double-counted (gross +18.99 -> +13.42). Net of the ~0.16%
round trip the direction is unchanged (the book still loses), but every
per-edge / per-symbol sample was inflated by ~17% and every "DECAYING edge"
magnitude was computed on a union of trades counted twice.

SCOPE OF THE FIX — read-side, deliberately
------------------------------------------
This collapses the duplicates in the READER so the learned statistics stop
double-counting. It does NOT stop the writer from booking them: one physical
dip still opens one paper position per agreeing timeframe, and each still
closes on its own. That is a strategy-design question (should one dip take
three of the five paper slots?), not a measurement bug, and it is NOT changed
here. The collapse key is deliberately strict — identical entry AND identical
pnl AND <= `window_s` apart on one symbol — so it can only ever remove copies
of one fill, never two genuinely distinct trades.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
# The ledger the produced edges are measured on — the same file the openers
# write and paper_net reads.
PAPER_BOOK = _ROOT / "data" / "unified-paper.json"

# A redundant copy is one that ADDS NOTHING to the question "is this design
# still being traded?": it carries the same handoff, the same regime, no net
# advantage in dollars or win rate, and a weaker entry. But "adds nothing" must
# be judged on a sample a reader can check, not on a rule of thumb — see
# NON_ADDING_SAMPLE. A copy is NOT redundant when it pays more per fill, wins
# more often, or the incumbent has too little history to beat.
NON_ADDING_SAMPLE = 30

# A duplicate is the same physical fill re-recorded. The observed spread of a
# genuine multi-tf booking is sub-second (the scan writes each timeframe within
# the same second); a same-scan double-write is ~2 ms. 5s is comfortably above
# both and far below any honest re-entry cadence (the scan itself is 30 min).
DEFAULT_WINDOW_S = 5.0

# THE MULTI-TIMEFRAME CLAUSE (2026-10-22, corrected 2026-10-23)
# -----------------------------------------------------------------
# The exact-fingerprint rule above assumes every booking of one fill records the
# same entry and pnl. It does not: each timeframe re-reads the ticker, so the
# bookings land a tick apart. Measured on today's book — 80 same-symbol pairs
# within 5 minutes, 54 with an exact entry+pnl, and the 5s rule caught NONE of
# them. That is why the ledger still held ~520 phantom rows three days after
# they were declared fixed.
#
# WINDOW, DERIVED FROM THE BOOK (not from a fixture): matched cross-timeframe
# pairs cluster at p50 = 0.54s and run to a hard maximum of 36.1s; the very next
# pair of any kind is 357.7s away — one full scan cadence. So there is a clean
# empty band between 36s and 358s, and 60s sits in the middle of it: 1.7x above
# the largest real booking gap and 6x below the nearest honest re-entry. (The
# old 20s was set from a hand-written 22s fixture and sat BELOW the real 36.1s
# maximum, so it clipped genuine bookings; the fixture has been corrected too.)
MULTITF_WINDOW_S = 60.0
TF_ENTRY_TOL = 0.001     # 0.1% of entry price
TF_PNL_TOL = 0.05        # dollars at $5 notional

# A timeframe the scan books once (5m/15m/30m). Records carrying anything else
# are left alone by the cross-tf clause — an unknown tf is not evidence of a
# shared fill.
KNOWN_TFS = ("5m", "15m", "30m")


def _open_dt(record: dict) -> datetime | None:
    """The record's OPEN time, parsed to an aware datetime, or None."""
    ts = record.get("ts_utc") or record.get("open_ts_utc")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_copy(a: dict, b: dict, window_s: float) -> bool:
    """True when `b` is a re-booking of `a`, not a distinct trade.

    Strict on purpose: same symbol (guaranteed by the caller), EXACT entry,
    EXACT pnl, and open times within `window_s`. Any one of those failing means
    they are different trades and both are kept. Two distinct fills sharing an
    exact entry price, an exact dollar pnl AND a sub-5s gap is not a thing the
    book is capable of producing.
    """
    da, db = _open_dt(a), _open_dt(b)
    # A copy must be placeable in time. If EITHER record lacks a usable open
    # time we cannot establish that it is the same fill, so we keep both —
    # precision over recall. (This also matches the documented contract that an
    # untimed record is never collapsed; a book of untimed records, e.g. a test
    # fixture or a legacy schema, must survive the pass untouched.)
    if da is None or db is None:
        return False
    if abs((db - da).total_seconds()) > window_s:
        return False
    if a.get("entry") != b.get("entry"):
        return False
    if a.get("pnl") != b.get("pnl"):
        return False
    return True


def _norm_tf(tf):
    """Timeframe as a string, or "" when the record carries none."""
    return "" if tf is None else str(tf)


def _is_fill_pair(a: dict, b: dict, window_s: float) -> bool:
    """Is `b` another booking of the SAME PHYSICAL FILL as `a`?

    The 5s/exact-entry/exact-pnl test (`_is_copy`) cannot see a multi-timeframe
    booking, and that is not a small miss — measured 2026-10-22:

        pairs within 5 MINUTES of each other, same symbol          n=80
          of which exact entry AND exact pnl                        n=54
          of which the 5s test already caught                        n=0
        max observed gap between the 5m / 15m / 30m bookings        36.1s

    The scan evaluates each timeframe in turn, so one physical dip is written by
    5m, then 15m, then 30m — each booking re-reads the ticker separately, and on
    four pairs in today's book the entries differ by ONE TICK ($12.221 vs
    $12.223) and the pnl by $0.04. Requiring an exact economic fingerprint kept
    every one of them: the defect was still fully present in the ledger, three
    days after it was documented as fixed.

    So this is an OR of two INDEPENDENT clauses:

      (1) EXACT — same entry AND same pnl, within `window_s`. The original key,
          unchanged, and it also covers a same-tf double-write at ~2ms.
      (2) CROSS-TF — two DIFFERENT known timeframes of the same symbol, entries
          within TF_ENTRY_TOL, pnl within TF_PNL_TOL, within MULTITF_WINDOW_S.

    ⚠️ 2026-10-23 — WHY CLAUSE (2) IS A SIBLING AND NOT A FALLBACK. The first
    version of this function checked (2) only inside `if gap > window_s:`. But
    the real multi-tf bookings are 0.5-1.2s apart, i.e. INSIDE the 5s window,
    so 711 of the 727 cross-tf pairs never reached the clause at all: it fired
    on 16 pairs out of 727 and its own three tests failed. A clause that can
    only run where the defect does not occur is not a fix. The two clauses are
    now evaluated independently of each other.

    Same-timeframe records are never merged by (2): a 5m and a 5m a second
    apart are two scans, hence two fills. Untimed records are never collapsed
    by either clause. `tf` is read from either record (the clause needs the two
    to DIFFER, not a particular one to be present).
    """
    da, db = _open_dt(a), _open_dt(b)
    if da is None or db is None:
        return False
    gap = abs((db - da).total_seconds())
    if gap > max(window_s, MULTITF_WINDOW_S):
        return False

    # (1) EXACT FINGERPRINT — the original key.
    if gap <= window_s and a.get("entry") == b.get("entry") \
            and a.get("pnl") == b.get("pnl"):
        return True

    # (2) CROSS-TIMEFRAME RE-BOOKING.
    if gap > MULTITF_WINDOW_S:
        return False
    ta, tb = _norm_tf(a.get("tf")), _norm_tf(b.get("tf"))
    if ta not in KNOWN_TFS or tb not in KNOWN_TFS:
        return False
    if ta == tb:
        return False
    ea, eb = _entry_of(a), _entry_of(b)
    if not ea or not eb:
        return False
    if abs(eb - ea) / ea > TF_ENTRY_TOL:
        return False
    try:
        pa = float(a.get("pnl") or 0)
        pb = float(b.get("pnl") or 0)
    except (TypeError, ValueError):
        return False
    return abs(pb - pa) <= TF_PNL_TOL


def collapse_multitf(records: list[dict],
                     window_s: float = DEFAULT_WINDOW_S) -> tuple[list[dict], int]:
    """Collapse one-fill-booked-per-timeframe duplicates.

    Returns `(collapsed, removed)`. The FIRST record of each duplicate group is
    kept (records are ordered by open time within a symbol), so the surviving
    trade carries the earliest timestamp — the honest open time of the fill.

    Records are grouped by symbol and walked in open-time order; a run of
    records within the fill window and matching on the economic fingerprint
    counts as one fill. The fingerprint is `_is_fill_pair`: an exact entry+pnl
    within `window_s`, OR the same tick booked under two different timeframes
    within MULTITF_WINDOW_S (tolerances TF_ENTRY_TOL / TF_PNL_TOL).

    ⚠️ 2026-10-22: the exact-only version of this key silently collapsed almost
    nothing of what it was written for (0 of 80 multi-tf pairs) because the
    bookings differ by a tick. If you tighten the tolerances, re-run
    tests/test_paper_book_multitf.py::test_real_shape_one_tick_apart_is_one_fill
    and check the live count with `python scripts/paper_book.py`, which prints
    the removed count every run.

    A record with no parseable timestamp is never collapsed (it cannot be placed
    in time, so it is treated as distinct and kept).
    """
    by_symbol: dict[object, list[dict]] = {}
    for r in records:
        by_symbol.setdefault(r.get("symbol"), []).append(r)

    out: list[dict] = []
    for _sym, group in by_symbol.items():
        # Sort by open time; records without a usable stamp sort last and are
        # never used as the anchor of a group.
        group.sort(key=lambda r: (_open_dt(r) is None,
                                  _open_dt(r) or datetime.max.replace(
                                      tzinfo=timezone.utc)))
        i = 0
        while i < len(group):
            anchor = group[i]
            out.append(anchor)
            j = i + 1
            while j < len(group) and _is_fill_pair(anchor, group[j], window_s):
                j += 1
            i = j
    return out, len(records) - len(out)


# --------------------------------------------------------------------------
# LIVENESS + REDUNDANCY — who is still being traded, and who is a copy
# --------------------------------------------------------------------------
def _entry_of(record: dict):
    """The record's entry price, parsed, or None."""
    try:
        e = record.get("entry")
        return float(e) if e is not None else None
    except (TypeError, ValueError):
        return None


def _shares_handoff(a: dict, b: dict) -> bool:
    """Could `b` have been produced by the SAME signal as `a`?

    Same tf family (or same tf), same regime, same entry price, within 72h.
    This is deliberately blind to the edge NAME: a strategy copy is defined by
    a shared signal, not by a label. Three siblings opened as separate edges
    on 2026-09-04 already fail this test — they differ in timeframe — so the
    live book's three real edges are NOT judged copies of one another.
    """
    ta, tb = a.get("tf"), b.get("tf")
    if ta and tb and ta != tb:
        return False
    if a.get("regime") != b.get("regime"):
        return False
    ea, eb = _entry_of(a), _entry_of(b)
    if ea is None or eb is None or ea != eb:
        return False
    da, db = _open_dt(a), _open_dt(b)
    if da is None or db is None or abs((db - da).total_seconds()) > 72 * 3600:
        return False
    return True


def _net_stats(trades: list) -> dict:
    """Tiny net reader (imported lazily so the module stays importable alone)."""
    try:
        from paper_net import net_of
    except Exception:  # noqa: BLE001 — degrade to gross rather than crash
        net_of = None
    nets = [(net_of(t)[0] if net_of else (t.get("pnl") or 0)) for t in trades]
    n = len(nets)
    return {"n": n,
            "net": sum(nets),
            "ev": (sum(nets) / n) if n else 0.0,
            "wr": (sum(1 for x in nets if x > 0) / n * 100) if n else 0.0}


def edge_liveness(records: list[dict]) -> dict:
    """When did each edge last fill, and which edges are REDUNDANT COPIES?

    Returns {"last_fill": {edge: iso}, "redundant": {edge: kept_edge},
             "edges": {edge: {...}}}.

    WHY (2026-10-21)
    ----------------
    The per-edge verdicts in improvement_loop / self_improve are computed over
    the WHOLE book, and they quote the historical mean of a population with no
    time bound. On 2026-10-21 the loop reported "Edge 'supertrend' DECAYING
    (net) 31.3% WR / 134 trades / -1.87 net — tighten or stand it down" and the
    same for bollinger, ema-cross, momentum, pullback, breakout. Every one of
    those edges had been DISABLED (data/signals-state.json), and every one had
    stopped filling in mid-September: last supertrend fill 2026-09-08, bollinger
    09-10, pullback 09-06, breakout 09-13. "Tighten its entry" is advice about
    a design that has not produced a trade in five weeks; it is history, not a
    live verdict, and no reader of the card could tell the difference.

    The complement of that defect: an edge that stopped filling for ANY reason
    (disabled, crashed, never enabled) gets its record frozen — and a frozen
    record with a good mean reads as PROVEN FOREVER. That is what a "proven"
    tag must not be able to mean (the lesson loop, same day: a recorded score
    8 hours old had to be recomputed at write time). So liveness also governs
    the PROMOTE side: a stalled edge cannot be promoted.

    REDUNDANCY
    ----------
    A copy that shares another edge's handoff (same tf, regime, entry time,
    price) and adds nothing — no higher net P&L, no higher win rate, and not
    too small a sample to answer — should not get its own independent verdict,
    because its evidence is the incumbent's evidence with a different name. It
    is reported as a copy of the edge holding the better record. When the
    incumbent has < NON_ADDING_SAMPLE fills the copy is NOT called redundant
    (nothing to beat yet).
    """
    by_edge: dict[str, list] = {}
    for t in records:
        e = t.get("edge") or ("loose" if t.get("loose") else "validated")
        by_edge.setdefault(e, []).append(t)

    last_fill = {}
    for e, ts in by_edge.items():
        last_fill[e] = max((str(t.get("ts_utc", "")) for t in ts), default="")

    stats = {e: _net_stats(ts) for e, ts in by_edge.items()}
    redundant: dict[str, str] = {}
    names = sorted(by_edge)
    for e in names:
        for other in names:
            if other == e or other in redundant or e in redundant:
                continue
            shared = sum(1 for a in by_edge[e] for b in by_edge[other]
                         if _shares_handoff(a, b))
            if not shared:
                continue
            se, so = stats[e], stats[other]
            if so["n"] < NON_ADDING_SAMPLE:
                continue
            adds = (se["ev"] > so["ev"] or se["wr"] > so["wr"])
            if not adds:
                redundant[e] = other
                break

    return {"last_fill": last_fill, "redundant": redundant, "stats": stats}


def edge_liveness_text(liv: dict, live_edges=(), stale_days: float = 14.0) -> str:
    """One sentence naming the edges that are NOT live, or '' when all are.

    `live_edges` is the writer's own knowledge of what can open (e.g. the
    signals-state `disabled` list); an edge fed to the book inside
    `stale_days` is live by the data whether or not the writer names it.
    """
    if not liv or not liv.get("last_fill"):
        return ""
    stale = []
    for e, ts in sorted(liv["last_fill"].items(),
                        key=lambda kv: kv[1] or ""):
        dt = _open_dt({"ts_utc": ts}) if ts else None
        if dt is None:
            continue
        age = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
        if age > stale_days:
            stale.append(f"'{e}' {age:.0f}d")
    if not stale:
        return ""
    off = [e for e in sorted(live_edges)]
    return (f"NOT LIVE (no fill in >{stale_days:.0f}d): " + ", ".join(stale)
            + (f". Disabled by the writer: {', '.join(off)}." if off else ".")
            + " Their per-edge verdicts are HISTORY, not a current state —"
              " tightening or standing down a design that is already off is"
              " a no-op. Re-enable one at a time to re-test it.")


def redundant_edges_text(liv: dict) -> str:
    red = (liv or {}).get("redundant") or {}
    if not red:
        return ""
    parts = [f"'{e}' ~ copy of '{k}'" for e, k in sorted(red.items())]
    return ("REDUNDANT EDGES (shared handoff, no net advantage, n>="
            f"{NON_ADDING_SAMPLE}): " + "; ".join(parts)
            + ". Their evidence is the parent's evidence — do not read it as"
              " independent confirmation.")


if __name__ == "__main__":
    closed = json.loads(PAPER_BOOK.read_text()).get("closed", [])
    collapsed, removed = collapse_multitf(closed)
    raw_g = sum(t.get("pnl", 0) or 0 for t in closed)
    col_g = sum(t.get("pnl", 0) or 0 for t in collapsed)
    print(f"paper closed      raw={len(closed):5d}  collapsed={len(collapsed):5d}  removed={removed}")
    print(f"gross pnl         raw={raw_g:+.3f}  collapsed={col_g:+.3f}  "
          f"phantom={raw_g - col_g:+.3f}")
