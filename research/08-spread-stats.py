#!/usr/bin/env python3
"""Measured spreads, top-of-book depth and taker fit per symbol, from recorded ticks.

Reads only CLOSED hours (`<root>/<date>/{book,trade}/<HH>.jsonl.gz`); the hour still being
appended is a plain `.jsonl` and is skipped. Read-only: nothing is written anywhere.

What it measures, per symbol:
- relative spread (ask - bid) / mid in bp: median, p90, p99 **time-weighted** (each book
  record is weighted by how long it stood, capped at MAX_HOLD_S so a gap or reconnect cannot
  stretch one quote over minutes), plus the event-weighted median for comparison;
- median top-of-book depth in USD at bid and ask (time-weighted);
- taker fit for a $5 and a $50 order: the share of time, and the share of real trades, where
  that notional is larger than the L1 quantity on the side it would take. Within L1 the extra
  slippage beyond the best price is 0; above it the result is only a FLAG ("exceeds top
  level"). There is no depth beyond L1 in the recording, so no deeper price is invented.
- implied one-way feed latency: trade `ts_recv` (local clock) minus exchange `trade_ms`,
  corrected by the measured clock offset in `clock-offset.jsonl`.

Limits (stated, not hidden): the recorder keeps bookTicker updates whose best PRICE moved
plus about one snapshot per second, so quantity-only changes between kept records are
lost and L1 depth is a sampled approximation. `ts_recv` is the local clock, which runs about
5.4 s ahead of the exchange; that cancels in spreads and durations, not in latency.

Run:
    python research/08-spread-stats.py --root <ticks dir>                 # spread table
    python research/08-spread-stats.py --root <ticks dir> --breakeven    # + break-even table
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

MAX_HOLD_S = 5.0            # a record never stands longer than this (gap / reconnect guard)
MAX_BOOK_AGE_S = 5.0        # a trade is matched to a book record at most this old
ORDER_SIZES_USD = (5.0, 50.0)


# ---------------------------------------------------------------- reading
def closed_files(root: Path, kind: str) -> list[Path]:
    """Closed hourly files for one stream kind, in time order. Open `.jsonl` hours are skipped."""
    return sorted(Path(root).glob(f"*/{kind}/*.jsonl.gz"))


def iter_records(paths):
    for p in paths:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)


def load_book(paths) -> dict[str, list[tuple[int, float, float, float, float]]]:
    """symbol -> [(ts_recv_ns, bid, bid_qty, ask, ask_qty)] sorted by time."""
    out: dict[str, list] = defaultdict(list)
    for r in iter_records(paths):
        if r.get("kind") != "book":
            continue
        d = r["data"]
        out[r["symbol"]].append((r["ts_recv_ns"], d["bid"], d["bid_qty"], d["ask"], d["ask_qty"]))
    for rows in out.values():
        rows.sort(key=lambda x: x[0])
    return dict(out)


def load_trades(paths) -> dict[str, list[tuple[int, int, float, float, bool]]]:
    """symbol -> [(ts_recv_ns, trade_ms, price, qty, buyer_is_maker)] sorted by recv time."""
    out: dict[str, list] = defaultdict(list)
    for r in iter_records(paths):
        if r.get("kind") != "trade":
            continue
        d = r["data"]
        out[r["symbol"]].append((r["ts_recv_ns"], d["trade_ms"], d["price"], d["qty"], d["buyer_is_maker"]))
    for rows in out.values():
        rows.sort(key=lambda x: x[0])
    return dict(out)


# ---------------------------------------------------------------- core math
def rel_spread_bp(bid: float, ask: float) -> float:
    """(ask - bid) / mid in basis points."""
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid * 1e4


def weighted_percentile(values, weights, q: float) -> float:
    """Smallest value whose cumulative weight reaches q (0..1) of the total. Weights >= 0."""
    v = np.asarray(values, dtype=float)
    w = np.asarray(weights, dtype=float)
    keep = w > 0
    v, w = v[keep], w[keep]
    if v.size == 0:
        return float("nan")
    order = np.argsort(v, kind="stable")
    v, w = v[order], w[order]
    cum = np.cumsum(w)
    idx = int(np.searchsorted(cum, q * cum[-1], side="left"))
    return float(v[min(idx, v.size - 1)])


def hold_durations_s(ts_ns, max_hold_s: float = MAX_HOLD_S) -> np.ndarray:
    """How long each record stood (until the next one), capped. The last record gets 0."""
    t = np.asarray(ts_ns, dtype=np.int64)
    if t.size == 0:
        return np.zeros(0)
    d = np.diff(t).astype(float) / 1e9
    d = np.minimum(np.maximum(d, 0.0), max_hold_s)
    return np.append(d, 0.0)


def book_stats(rows, sizes_usd=ORDER_SIZES_USD, max_hold_s: float = MAX_HOLD_S) -> dict:
    """Spread and L1-depth statistics for one symbol's book rows (see load_book)."""
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    bid = np.array([r[1] for r in rows], dtype=float)
    bq = np.array([r[2] for r in rows], dtype=float)
    ask = np.array([r[3] for r in rows], dtype=float)
    aq = np.array([r[4] for r in rows], dtype=float)
    w = hold_durations_s(ts, max_hold_s)
    valid = ask > bid                               # locked/crossed quotes are counted, not used
    mid = (bid + ask) / 2.0
    spread_bp = np.where(valid, (ask - bid) / np.where(mid > 0, mid, 1.0) * 1e4, np.nan)
    wv = np.where(valid, w, 0.0)
    bid_usd, ask_usd = bid * bq, ask * aq
    raw_spread = np.where(valid, ask - bid, np.nan)
    min_spread = float(np.nanmin(raw_spread)) if valid.any() else float("nan")
    at_min = np.isclose(raw_spread, min_spread, rtol=0, atol=min_spread * 1e-6) & valid
    total_w = float(wv.sum())
    out = {
        "n": int(len(rows)),
        "invalid": int((~valid).sum()),
        "capped": int((np.diff(ts) / 1e9 > max_hold_s).sum()) if ts.size > 1 else 0,
        "hours": total_w / 3600.0,
        "first_ns": int(ts.min()) if ts.size else None,
        "last_ns": int(ts.max()) if ts.size else None,
        "med_bp": weighted_percentile(spread_bp[valid], wv[valid], 0.50),
        "p90_bp": weighted_percentile(spread_bp[valid], wv[valid], 0.90),
        "p99_bp": weighted_percentile(spread_bp[valid], wv[valid], 0.99),
        "med_bp_event": float(np.median(spread_bp[valid])) if valid.any() else float("nan"),
        "min_spread_abs": min_spread,
        "share_at_min_spread": float(wv[at_min].sum() / total_w) if total_w else float("nan"),
        "med_bid_usd": weighted_percentile(bid_usd, w, 0.50),
        "med_ask_usd": weighted_percentile(ask_usd, w, 0.50),
    }
    for s in sizes_usd:
        # share of TIME where an order of s USD is larger than the L1 quantity it would take
        out[f"t_exceeds_buy_{s:g}"] = float(w[ask_usd < s].sum() / w.sum()) if w.sum() else float("nan")
        out[f"t_exceeds_sell_{s:g}"] = float(w[bid_usd < s].sum() / w.sum()) if w.sum() else float("nan")
    return out


def taker_fit_at_trades(book_rows, trade_rows, sizes_usd=ORDER_SIZES_USD,
                        max_age_s: float = MAX_BOOK_AGE_S) -> dict:
    """At each real trade, would a taker order of s USD on the aggressor's side fit in L1?

    buyer_is_maker False = the taker bought = consumed the ask; True = sold into the bid.
    The book record used is the latest one received at or before the trade (same local clock).
    Returns the matched count and, per size, the share that exceeds the top level.
    """
    bts = np.array([r[0] for r in book_rows], dtype=np.int64)
    bid_usd = np.array([r[1] * r[2] for r in book_rows], dtype=float)
    ask_usd = np.array([r[3] * r[4] for r in book_rows], dtype=float)
    matched = 0
    exceeds = {s: 0 for s in sizes_usd}
    for tr in trade_rows:
        i = int(np.searchsorted(bts, tr[0], side="right")) - 1
        if i < 0 or (tr[0] - bts[i]) / 1e9 > max_age_s:
            continue
        matched += 1
        depth = bid_usd[i] if tr[4] else ask_usd[i]
        for s in sizes_usd:
            if s > depth:
                exceeds[s] += 1
    return {"matched": matched,
            **{f"trade_exceeds_{s:g}": (exceeds[s] / matched if matched else float("nan")) for s in sizes_usd}}


def slippage_beyond_best(order_usd: float, l1_usd: float):
    """Extra cost beyond the opposite best price: 0.0 within L1, else the string flag."""
    return 0.0 if order_usd <= l1_usd else "exceeds top level"


def implied_latency_ms(trade_rows, offsets_ms) -> dict:
    """recv_local - trade_exchange, corrected by the clock offset (server - local)."""
    lags = [tr[0] / 1e6 - tr[1] for tr in trade_rows]
    off = statistics.median(offsets_ms) if offsets_ms else float("nan")
    lag_med = statistics.median(lags) if lags else float("nan")
    return {"lag_raw_ms": lag_med, "offset_ms": off, "latency_ms": lag_med + off}


def load_clock_offsets(path: Path) -> tuple[list[float], list[float]]:
    offs, rtts = [], []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                offs.append(r["offset_ms"])
                rtts.append(r["rtt_ms"])
    return offs, rtts


# ---------------------------------------------------------------- report
def _utc(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def breakeven_rows(spreads_bp: dict[str, float]) -> None:
    """Re-run research/07-breakeven.py with measured half-spreads as slip per side."""
    spec = importlib.util.spec_from_file_location("breakeven", Path(__file__).with_name("07-breakeven.py"))
    be = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(be)
    cols = [("slip 0", 0.0)] + [(k, v / 2 / 1e4) for k, v in spreads_bp.items()]
    for name, fee in be.FEE_SCENARIOS.items():
        print(f"\n**{name}** (slip per side = half the measured spread)\n")
        print("| TP / SL | " + " | ".join(c for c, _ in cols) + " |")
        print("|---|" + "---|" * len(cols))
        for tp, sl in be.TP_SL:
            cells = []
            for _, s in cols:
                p = be.breakeven_win_rate(tp, sl, be.round_trip_cost(fee, s))
                cells.append("IMPOSS." if p >= 1 else f"{p * 100:.1f}%")
            print(f"| {tp * 100:.2f} / {sl * 100:.2f}% | " + " | ".join(cells) + " |")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", required=True, help="ticks dir, e.g. data/local/ticks")
    ap.add_argument("--clock", default=None, help="clock-offset.jsonl (default: <root>/../clock-offset.jsonl)")
    ap.add_argument("--breakeven", action="store_true", help="also print the break-even table")
    a = ap.parse_args()
    root = Path(a.root)
    book_files, trade_files = closed_files(root, "book"), closed_files(root, "trade")
    print(f"closed book files: {len(book_files)} ({book_files[0].parent.parent.name}/{book_files[0].name} .. "
          f"{book_files[-1].parent.parent.name}/{book_files[-1].name}); closed trade files: {len(trade_files)}")
    book = load_book(book_files)
    trades = load_trades(trade_files)
    first = min(r[0][0] for r in book.values())
    last = max(r[-1][0] for r in book.values())
    print(f"window (local recv clock, UTC): {_utc(first)} .. {_utc(last)} = {(last - first) / 3.6e12:.2f} h\n")

    stats = {s: book_stats(rows) for s, rows in sorted(book.items())}
    fits = {s: taker_fit_at_trades(book[s], trades.get(s, [])) for s in stats}

    print("| Symbol | book records | hours observed | median bp | p90 bp | p99 bp | median bp (event-weighted) "
          "| time at min observed spread (~1 tick) | median bid depth $ | median ask depth $ |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for s, st in stats.items():
        print(f"| {s} | {st['n']:,} | {st['hours']:.2f} | {st['med_bp']:.4f} | {st['p90_bp']:.4f} | "
              f"{st['p99_bp']:.4f} | {st['med_bp_event']:.4f} | {_pct(st['share_at_min_spread'])} | "
              f"{st['med_bid_usd']:,.0f} | {st['med_ask_usd']:,.0f} |")

    print("\nTaker fit: share where the order is LARGER than L1 on the side it takes "
          "(0 extra slippage otherwise; above L1 = 'exceeds top level', price UNKNOWN)\n")
    print("| Symbol | trades matched | $5 buy, % time | $5 sell, % time | $50 buy, % time | $50 sell, % time "
          "| $5 at real trades | $50 at real trades |")
    print("|---|---|---|---|---|---|---|---|")
    for s, st in stats.items():
        f = fits[s]
        print(f"| {s} | {f['matched']:,} | {_pct(st['t_exceeds_buy_5'])} | {_pct(st['t_exceeds_sell_5'])} | "
              f"{_pct(st['t_exceeds_buy_50'])} | {_pct(st['t_exceeds_sell_50'])} | "
              f"{_pct(f['trade_exceeds_5'])} | {_pct(f['trade_exceeds_50'])} |")

    invalid = sum(st["invalid"] for st in stats.values())
    capped = sum(st["capped"] for st in stats.values())
    print(f"\nlocked/crossed book records excluded: {invalid}; hold intervals capped at {MAX_HOLD_S:g}s: {capped}")

    clock = Path(a.clock) if a.clock else root.parent / "clock-offset.jsonl"
    offs, rtts = load_clock_offsets(clock)
    all_trades = [t for rows in trades.values() for t in rows]
    lat = implied_latency_ms(all_trades, offs)
    if offs:
        print(f"latency: median raw lag (recv_local - trade_ms) {lat['lag_raw_ms']:.0f} ms; median clock offset "
              f"(server - local) {lat['offset_ms']:.0f} ms over {len(offs)} samples (median RTT "
              f"{statistics.median(rtts):.0f} ms, so offset uncertainty ~ +/-{statistics.median(rtts) / 2:.0f} ms); "
              f"implied one-way latency {lat['latency_ms']:.0f} ms")
    else:
        print("latency: UNKNOWN (no clock-offset file)")

    meds = {s: st["med_bp"] for s, st in stats.items()}
    all_med = statistics.median(meds.values())
    print(f"\nmedian-of-symbol medians: {all_med:.3f} bp; tightest {min(meds, key=meds.get)} "
          f"{min(meds.values()):.4f} bp; widest {max(meds, key=meds.get)} {max(meds.values()):.3f} bp; "
          f"widest p90 {max(st['p90_bp'] for st in stats.values()):.3f} bp")
    if a.breakeven:
        widest = max(meds, key=meds.get)
        widest_p90 = max(stats, key=lambda s: stats[s]["p90_bp"])
        breakeven_rows({
            f"tightest median ({min(meds, key=meds.get)} {min(meds.values()):.4f}bp)": min(meds.values()),
            f"median of symbols ({all_med:.2f}bp)": all_med,
            f"widest median ({widest} {meds[widest]:.2f}bp)": meds[widest],
            f"widest p90 ({widest_p90} {stats[widest_p90]['p90_bp']:.2f}bp)": stats[widest_p90]["p90_bp"],
        })


if __name__ == "__main__":
    main()
