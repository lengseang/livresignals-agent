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


def load(fname, data_dir=None):
    p = (Path(data_dir) if data_dir is not None else D) / fname
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
    ("trend-state.json", "log", "trend_follower (REAL $)",
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


# ---------------------------------------------------------------------------
# TAIL BESIDE THE MEAN (T0.23)
#
# A trend-follower's arithmetic mean is a statement about its right tail until
# proven otherwise. On the real trend book the mean net was +1.050%/trade while
# the median trade lost 1.324% and deleting the single best row left +0.003%
# (T0.22). Printed alone, the mean is read as "the typical trade". It is the
# opposite. These helpers are pure and shared by every reader, so two readers
# cannot come to disagree about the shape of the same book.
# ---------------------------------------------------------------------------

def _finite(xs):
    """Drop non-finite values (NaN/inf). A NaN return must never poison a
    mean or a median into printing 'nan'."""
    out = []
    for x in xs:
        if isinstance(x, bool):
            continue
        try:
            f = float(x)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f)
    return out


def median(xs):
    """Median of xs. Even n -> mean of the two central values (the standard
    definition, stated here so a reader need not guess). Returns None for an
    empty (or all non-finite) input — never a fabricated 0.0."""
    xs = sorted(_finite(xs))
    n = len(xs)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2.0


def delete_best_mean(xs):
    """Mean of xs after removing its single largest element ("delete-1").

    Returns None for n < 2: there is no mean of the remaining trades when
    there are none. Callers must print n/a, not a number."""
    xs = _finite(xs)
    n = len(xs)
    if n < 2:
        return None
    return (sum(xs) - max(xs)) / (n - 1)


def tail_note(xs, fee=0.0, subject="this book"):
    """Short note when a positive mean rests on <=1 trade, else ''.

    Fires only when mean_net > 0 AND delete1_net <= 0: deleting the single
    best trade turns the book non-positive. Silent otherwise (a broad edge, or
    a book whose mean already reads negative).

    `subject` names what the mean describes ("this book" / "this symbol"), so
    the per-symbol reader does not call a symbol a book.

    Wording is deliberately two clauses so it survives a narrow terminal and a
    chat window: (1) what the mean is, (2) that it is not the typical trade."""
    xs = _finite(xs)
    n = len(xs)
    if n < 2:
        return ""
    mean_net = sum(xs) / n - fee
    d1 = delete_best_mean(xs)
    if d1 is None:
        return ""
    d1_net = d1 - fee
    if mean_net > 0 >= d1_net:
        return (f"⚠️ tail, not the middle: delete the single best trade and "
                f"{subject}'s net mean is non-positive.")
    return ""


def report_lines(data_dir=None):
    """Render the whole report as a list of lines. Pure: it reads the book
    files and returns text — no printing, so a test can assert on it.

    T0.23: every book row now carries `median NET` and a `delete-1` mean
    beside the arithmetic mean, and a book whose positive mean rests on <=1
    trade gets a tail note under the row and again in the diagnosis.
    """
    out = []
    out.append("=" * 100)
    out.append("EXPECTANCY vs FEE — a book must beat the toll to exist")
    out.append("=" * 100)
    out.append(f"  fee per round trip: {FEE_RT*100:.4f}%   "
               f"(-> {FEE_RT_NOBNB*100:.4f}% when the BNB runway ends)")
    out.append("")
    # `med/net` are NET of the fee, like `net/trade`; `d1` is the NET mean
    # after deleting the single best trade.
    out.append(f"  {'book':30s} {'n':>5s} {'avg gross':>10s} {'net/trade':>10s} "
               f"{'median':>9s} {'d1 mean':>9s} {'t':>6s} {'win%':>6s}  verdict")
    out.append("  " + "-" * 104)

    rows = []
    for fname, key, label, kw in BOOKS:
        d = load(fname, data_dir)
        if not d:
            out.append(f"  {label:30s}  (file missing)")
            continue
        recs = d.get(key) or []
        rs, skipped, derived = to_rates(recs, **kw)
        if not rs:
            out.append(f"  {label:30s} {len(recs):5d} {skipped:5d}   no usable returns")
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
        med = median(rs)
        med_net = (med - FEE_RT) if med is not None else None
        d1 = delete_best_mean(rs)
        d1_net = (d1 - FEE_RT) if d1 is not None else None
        note = tail_note(rs, FEE_RT)
        rows.append((label, n, skipped, avg, net, t, wr, v, derived,
                     med_net, d1_net, note))
        dnote = f" ({derived} derived)" if derived else ""
        med_s = f"{med_net*100:+8.3f}%" if med_net is not None else "     n/a"
        d1_s = f"{d1_net*100:+8.3f}%" if d1_net is not None else "     n/a"
        out.append(f"  {label:30s} {n:5d} {avg*100:+9.3f}% {net*100:+9.3f}% "
                   f"{med_s} {d1_s} {t:+6.2f} {wr:5.1f}% {v}{dnote}")
        if note:
            out.append(f"  {'':30s} {note}")

    out.append("")
    out.append("=" * 100)
    out.append("DIAGNOSIS — highest-cost book first")
    out.append("=" * 100)
    for (label, n, skipped, avg, net, t, wr, v, derived,
         med_net, d1_net, note) in sorted(rows, key=lambda r: r[4]):
        if v.startswith("DEAD") or v == "loses to fees":
            out.append(f"  🔴 {label}")
            out.append(f"     {n} trades | gross {avg*100:+.3f}%/trade | win {wr:.0f}% | "
                       f"fee {FEE_RT*100:.3f}% | NET {net*100:+.3f}%/trade | t={t:+.2f}")
            if abs(t) < 2:
                out.append(f"     t≈0 => the gross edge is indistinguishable from ZERO.")
                out.append(f"     The signal carries no information; fees do the damage. "
                           f"Fewer, larger trades is the only lever.")
            else:
                out.append(f"     t={t:+.2f} => significantly NEGATIVE. The signal is "
                           f"actively wrong, not merely weak.")
        else:
            mark = "⚠️ " if note else "✅ "
            out.append(f"  {mark}{label}: gross {avg*100:+.3f}%, net {net*100:+.3f}%, "
                       f"t={t:+.2f}, win {wr:.0f}%")
        if note:
            med_s = f"{med_net*100:+.3f}%" if med_net is not None else "n/a"
            d1_s = f"{d1_net*100:+.3f}%" if d1_net is not None else "n/a"
            out.append(f"     {note}")
            out.append(f"     NET mean {net*100:+.3f}% | median {med_s} | "
                       f"delete-1 mean {d1_s}")

    out.append("")
    out.append(f"  break-even gross return required: {FEE_RT*100:.4f}% per trade.")
    return out


def main():
    for line in report_lines():
        print(line)


if __name__ == "__main__":
    main()
