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

from datetime import datetime, timezone

# A duplicate is the same physical fill re-recorded. The observed spread of a
# genuine multi-tf booking is sub-second (the scan writes each timeframe within
# the same second); a same-scan double-write is ~2 ms. 5s is comfortably above
# both and far below any honest re-entry cadence (the scan itself is 30 min).
DEFAULT_WINDOW_S = 5.0


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


def collapse_multitf(records: list[dict],
                     window_s: float = DEFAULT_WINDOW_S) -> tuple[list[dict], int]:
    """Collapse one-fill-booked-per-timeframe duplicates.

    Returns `(collapsed, removed)`. The FIRST record of each duplicate group is
    kept (records are ordered by open time within a symbol), so the surviving
    trade carries the earliest timestamp — the honest open time of the fill.

    Records are grouped by symbol and walked in open-time order; a run of
    records all within `window_s` and sharing entry+pnl counts as one fill. A
    record with no parseable timestamp is never collapsed (it cannot be placed
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
            while j < len(group) and _is_copy(anchor, group[j], window_s):
                j += 1
            i = j
    return out, len(records) - len(out)


if __name__ == "__main__":
    import json
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "data" / "unified-paper.json"
    closed = json.loads(p.read_text()).get("closed", [])
    collapsed, removed = collapse_multitf(closed)
    raw_g = sum(t.get("pnl", 0) or 0 for t in closed)
    col_g = sum(t.get("pnl", 0) or 0 for t in collapsed)
    print(f"paper closed      raw={len(closed):5d}  collapsed={len(collapsed):5d}  removed={removed}")
    print(f"gross pnl         raw={raw_g:+.3f}  collapsed={col_g:+.3f}  "
          f"phantom={raw_g - col_g:+.3f}")
