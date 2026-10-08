#!/usr/bin/env python3
"""EXPECTANCY vs FEE — which books CANNOT win, and why.

THE ONE NUMBER THAT MATTERS
---------------------------
A book's gross edge per trade must exceed the cost per trade, or it loses no
matter how good the signal looks. Fees measured on this account: 0.1594% round
trip (BNB discount active), rising to 0.2000% when the BNB runway ends.

    net expectancy = avg gross return per trade - fee per round trip

Negative net with a t-stat near zero means the signal carries no information —
a different diagnosis from "significantly negative", with a different fix.

FIELD-NAME TRAP
---------------
Each book stores its return under a different key and in a different unit:
    trend/regime  'ret'      fraction  (-0.0231 = -2.31%)
    adaptive      'ret_pct'  percent   (-2.31   = -2.31%)
    unified       'pnl'      DOLLARS   -> needs size to become a rate
    futures       'pnl'      DOLLARS   -> needs notional
The first version of this script only read 'ret' and therefore silently skipped
three of the five books, reporting "all clear" on 40% of the data. A check that
cannot see a book must never imply the book is fine.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

D = Path("/opt/trading-agent/data")
FEE_RT = 0.001594
FEE_RT_NOBNB = 0.002000


def load(fname):
    p = D / fname
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def to_rates(recs, pct_key=None, frac_key=None, usd_key=None, size_key=None):
    """Return (rates, skipped, derived) where rates are fractions.

    Falls back to deriving the return from entry/exit prices when the stored
    field is absent. This is not a convenience — it is required for correctness.

    Measured: adaptive-state.json has `ret_pct` on only 1 of its 18 closed
    trades. The other 17 predate the field. A reader that trusts the field
    therefore reports the book on 1 trade, i.e. it silently discards 94% of the
    evidence and invites the conclusion that the book is "unproven" when in fact
    it is merely unread.

    adaptive_engine's own learner already derives the return from prices for
    those records (see _learn_from_closed_trades). This reader now does the same,
    so every book is measured on all of its data.
    """
    out, skipped, derived = [], 0, 0
    for t in recs:
        got = None
        if frac_key:
            v = t.get(frac_key)
            if isinstance(v, (int, float)):
                got = float(v)
        if got is None and pct_key:
            v = t.get(pct_key)
            if isinstance(v, (int, float)):
                got = float(v) / 100.0
        if got is None and usd_key:
            v = t.get(usd_key)
            base = t.get(size_key) if size_key else None
            if (isinstance(v, (int, float)) and isinstance(base, (int, float))
                    and base):
                got = float(v) / float(base)
        if got is None:
            # last resort: derive from prices
            e_, x_ = t.get("entry"), t.get("exit")
            if (isinstance(e_, (int, float)) and isinstance(x_, (int, float))
                    and e_):
                got = (float(x_) - float(e_)) / float(e_)
                derived += 1
        if got is None:
            skipped += 1
        else:
            out.append(got)
    return out, skipped, derived


BOOKS = [
    ("unified-paper.json", "closed", "unified_trader (paper, hi-freq)",
     dict(usd_key="pnl", size_key="size")),
    ("adaptive-state.json", "closed_trades", "adaptive_engine (paper)",
     dict(pct_key="ret_pct")),
    ("regime-state.json", "log", "regime_trader (DEMO, L/S)",
     dict(frac_key="ret")),
    # The REAL book's paper UNIVERSE (53 symbols, priced at candle close). The
    # label said "REAL $" until T0.10 — it is a paper log and readers must not
    # present it as the money book. The money book is real.closed, below.
    ("trend-state.json", "log", "trend_follower (paper universe)",
     dict(frac_key="ret")),
    ("futures-trades.json", "closed", "futures_executor (demo)",
     dict(usd_key="pnl", size_key="notional")),
]


def tstat(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return 0.0 if sd == 0 else m / (sd / math.sqrt(n))


def real_closed_lines(recs: list) -> list[str]:
    """The REAL money book (trend-state real.closed), reported honestly (T0.10).

    `pnl_usd` is already NET of fees when it is present, and null when entry,
    exit or fee were unknown at close — so those records are UNKNOWN, never
    $0.00. Every money figure is printed next to the count it could not price.
    """
    n_total = len(recs)
    known = [r for r in recs if isinstance(r.get("pnl_usd"), (int, float))]
    n_unknown = n_total - len(known)
    unknown_note = (f"  ({n_unknown} of {n_total} UNKNOWN — not counted)"
                    if n_unknown else "")
    if not known:
        if n_total == 0:
            return ["  trend_follower (REAL $): no real closes recorded yet."]
        return [f"  trend_follower (REAL $): no real closes with a known PnL "
                f"yet ({n_unknown} UNKNOWN)."]
    total = sum(r["pnl_usd"] for r in known)
    wins = [r["pnl_usd"] for r in known if r["pnl_usd"] > 0]
    wr = len(wins) / len(known) * 100
    out = [f"  trend_follower (REAL $): {len(known)} priced closes | "
           f"net {total:+.4f} USD | win {wr:.0f}%{unknown_note}"]
    for r in known:
        out.append(f"     {r.get('symbol', '?'):11s} {r['pnl_usd']:+8.4f} USD  "
                   f"({r.get('exit_status', '?')}/{r.get('entry_status', '?')})")
    return out


def main() -> None:
    print("=" * 100)
    print("EXPECTANCY vs FEE — a book must beat the toll to exist")
    print("=" * 100)
    print(f"  fee per round trip: {FEE_RT*100:.4f}%   "
          f"(-> {FEE_RT_NOBNB*100:.4f}% when the BNB runway ends)")
    print()
    hdr = (f"  {'book':34s} {'n':>5s} {'skip':>5s} {'avg gross':>10s} {'net/trade':>10s} "
           f"{'t':>6s} {'win%':>6s}  verdict")
    print(hdr)
    print("  " + "-" * 96)

    rows = []
    for fname, key, label, kw in BOOKS:
        d = load(fname)
        if not d:
            print(f"  {label:34s}  (file missing)")
            continue
        recs = d.get(key) or []
        rs, skipped, derived = to_rates(recs, **kw)
        if not rs:
            print(f"  {label:34s} {len(recs):5d} {skipped:5d}   no usable returns")
            continue
        n = len(rs)
        avg = sum(rs) / n
        net = avg - FEE_RT
        t = tstat(rs)
        wr = sum(1 for x in rs if x > 0) / n * 100
        if net <= 0 and abs(t) < 2:
            v = "DEAD — no edge, pays fees"
        elif net <= 0 and t < -2:
            v = "DEAD — significantly NEGATIVE"
        elif net <= 0:
            v = "loses to fees"
        elif abs(t) < 2:
            v = "alive, unproven"
        else:
            v = "positive & significant"
        rows.append((label, n, skipped, avg, net, t, wr, v, derived))
        dnote = f" ({derived} derived)" if derived else ""
        print(f"  {label:34s} {n:5d} {skipped:5d} {avg*100:+9.3f}% {net*100:+9.3f}% "
              f"{t:+6.2f} {wr:5.1f}% {v}{dnote}")

    print()
    print("=" * 100)
    print("DIAGNOSIS — highest-cost book first")
    print("=" * 100)
    for label, n, skipped, avg, net, t, wr, v, derived in sorted(rows, key=lambda r: r[4]):
        if v.startswith("DEAD") or v == "loses to fees":
            print(f"  🔴 {label}")
            print(f"     {n} trades | gross {avg*100:+.3f}%/trade | win {wr:.0f}% | "
                  f"fee {FEE_RT*100:.3f}% | NET {net*100:+.3f}%/trade | t={t:+.2f}")
            if abs(t) < 2:
                print(f"     t≈0 => the gross edge is indistinguishable from ZERO.")
                print(f"     The signal carries no information; fees do the damage. "
                      f"Fewer, larger trades is the only lever.")
            else:
                print(f"     t={t:+.2f} => significantly NEGATIVE. The signal is "
                      f"actively wrong, not merely weak.")
        else:
            print(f"  ✅ {label}: gross {avg*100:+.3f}%, net {net*100:+.3f}%, "
                  f"t={t:+.2f}, win {wr:.0f}%")

    print()
    print("=" * 100)
    print("REAL MONEY BOOK (fills, net of fees; not the paper universe)")
    print("=" * 100)
    trend = load("trend-state.json") or {}
    for ln in real_closed_lines((trend.get("real") or {}).get("closed") or []):
        print(ln)

    print()
    print(f"  break-even gross return required: {FEE_RT*100:.4f}% per trade.")


if __name__ == "__main__":
    main()
