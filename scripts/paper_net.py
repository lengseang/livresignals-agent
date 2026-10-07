#!/usr/bin/env python3
"""paper_net.py — the paper book's honest (net-of-cost) P&L, in one place.

WHY THIS EXISTS
---------------
On 2026-10-06 `unified_trader` began charging transaction costs and recording
`pnl_net` (gross minus cost) beside the gross `pnl`. But the autonomous
self-improve card kept reporting the GROSS figure, because it summed `pnl`,
which is unchanged by design. So the number the owner reads on the card was
still the flattered one — the very defect the cost change was made to fix.

This module is the single reader that answers "what did the paper book actually
make, after costs?" without hand-rolling the arithmetic at each call site.

THE MISSING-FIELD TRAP
----------------------
Only trades closed AFTER 2026-10-06 carry `pnl_net` (41 of ~2478 at the time of
writing). Summing `pnl_net` naively treats a missing field as 0, which would
understate the book and is exactly the class of silent-unit bug book_units.py
exists to stop. So this module:

  * uses `pnl_net` when the record has it (exact, authoritative), else
  * derives it as `pnl - size * PAPER_COST_RT`, the same formula `_book_pnl`
    uses, when `size` is present, else
  * falls back to gross and COUNTS the record as uncosted, never as a zero.

`PAPER_COST_RT` mirrors `unified_trader.PAPER_COST_RT` (0.0003 = 0.03% round
trip, the low end of the 30 real fills). It is a module constant so the two do
not silently drift; `unified_trader` remains the writer of record.
"""
from __future__ import annotations

# Must match unified_trader.PAPER_COST_RT (the writer). 0.03% round trip.
PAPER_COST_RT = 0.0003


def net_of(record: dict, cost_rt: float = PAPER_COST_RT) -> tuple[float, bool]:
    """Return (pnl_net_dollars, was_costed).

    `was_costed` is False only when neither `pnl_net` nor a usable `size` is
    present, i.e. the cost could not be recovered and a caller must not treat
    the value as net. Truthy in every other case, including a genuine `pnl_net`.
    """
    gross = record.get("pnl")
    if not isinstance(gross, (int, float)):
        return 0.0, False

    # 1. the writer already charged it — trust the recorded figure.
    pnl_net = record.get("pnl_net")
    if isinstance(pnl_net, (int, float)):
        return float(pnl_net), True

    # 2. charge it the same way _book_pnl would have.
    size = record.get("size")
    if isinstance(size, (int, float)) and size:
        return float(gross) - float(size) * cost_rt, True

    # 3. unrecoverable: gross only, and say so.
    return float(gross), False


def summarise(records: list[dict], cost_rt: float = PAPER_COST_RT) -> dict:
    """Aggregate a paper book: gross, net and the count that could not be costed.

    Net win-rate counts a trade as a win only if its NET is positive — a trade
    the cost turns from a winner into a loser is a loss, which is the whole point
    of measuring net.
    """
    n = len(records)
    if not n:
        return {"n": 0, "gross_pnl": 0.0, "net_pnl": 0.0, "wr_net": 0.0,
                "wr_gross": 0.0, "ev_net": 0.0, "uncosted": 0}
    gross = 0.0
    net = 0.0
    wg = 0
    wn = 0
    uncosted = 0
    for r in records:
        g = r.get("pnl")
        if isinstance(g, (int, float)):
            gross += g
            if g > 0:
                wg += 1
        nl, costed = net_of(r, cost_rt)
        net += nl
        if not costed:
            uncosted += 1
        if nl > 0:
            wn += 1
    return {
        "n": n,
        "gross_pnl": gross,
        "net_pnl": net,
        "wr_gross": wg / n * 100,
        "wr_net": wn / n * 100,
        "ev_net": net / n,
        "uncosted": uncosted,
    }


def per_symbol_net(records: list[dict], cost_rt: float = PAPER_COST_RT) -> dict:
    """Per-symbol net P&L in dollars, gross and net, plus win-rates."""
    out: dict[str, dict] = {}
    for r in records:
        sym = r.get("symbol")
        if not sym:
            continue
        d = out.setdefault(sym, {"n": 0, "gross": 0.0, "net": 0.0,
                                 "wr_gross": 0, "wr_net": 0.0})
        d["n"] += 1
        g = r.get("pnl")
        if isinstance(g, (int, float)):
            d["gross"] += g
            if g > 0:
                d["wr_gross"] += 1
        nl, _ = net_of(r, cost_rt)
        d["net"] += nl
        if nl > 0:
            d["wr_net"] += 1
    for d in out.values():
        if d["n"]:
            d["wr_gross"] = d["wr_gross"] / d["n"] * 100
            d["wr_net"] = d["wr_net"] / d["n"] * 100
    return out


if __name__ == "__main__":
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "data" / "unified-paper.json"
    closed = json.loads(p.read_text()).get("closed", [])
    s = summarise(closed)
    print(f"paper book n={s['n']} uncosted={s['uncosted']}")
    print(f"  GROSS pnl={s['gross_pnl']:+.3f} wr={s['wr_gross']:.1f}%")
    print(f"  NET   pnl={s['net_pnl']:+.3f} wr={s['wr_net']:.1f}% ev={s['ev_net']:+.5f}")
