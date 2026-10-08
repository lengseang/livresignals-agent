"""book_units.py — one canonical reading of every book's return field.

THE DEFECT THIS EXISTS TO PREVENT
On 2026-09-22 a recap script read `pnl` across all books and reported the
adaptive book at a **max drawdown of -251%** with an average win of +41%. Both
figures were fabricated by the script, not by the strategy: the fields written by
each book do not mean the same thing.

    writer              field      unit            example
    ------------------  ---------  --------------  ------------------------
    adaptive_engine     pnl        DOLLARS         -0.36  = -$0.36
                        ret_pct    fraction        -0.0036 = -0.36%
    futures_executor    pnl        fraction         0.0133 = +1.33%
    regime_trader       ret        fraction         0.00826 = +0.826%
    trend_follower      ret        fraction        -0.156  = -15.6%
    unified_trader      pnl        fraction         (per its writer)

Same field name, different units, in the same codebase. Any reader that assumes
one convention produces numbers that look plausible and are wrong — which is worse
than an error, because nothing flags it.

THE ACTUAL CONVENTIONS, each read from the expression that assigns it
--------------------------------------------------------------------
This table has now been wrong THREE times, always in the same direction: assuming
a unit instead of reading the writer. So each row cites the code that produces it.
Do not edit a row without reading the writer again.

    writer             field      unit      via
    -----------------  ---------  --------  ------------------------------------
    futures_executor   pnl        DOLLARS   (px - entry) * qty        [line ~388]
    adaptive_engine    pnl        DOLLARS   (px-entry)/entry * size
                       ret_pct    fraction  stored beside it (new records)
    unified_trader     pnl        DOLLARS   (px-entry) * (size/entry) [line ~200]
    regime_trader      ret        FRACTION  round(move, 5)
    trend_follower     ret        FRACTION  round(ret, 5)

THE THREE ERRORS, recorded so the pattern is visible
  1. Read adaptive's dollar `pnl` as a percent  -> reported -251% drawdown
  2. Read unified's dollar `pnl` as a percent   -> reported +1589% cum / -399% DD
  3. Read futures' dollar `pnl` as a FRACTION   -> reported -10.51% cum when the
     truth is -3.93%. This one survived a test suite, because the test asserted
     self-consistency rather than agreement with the writer. A test that checks
     your own arithmetic agrees with itself cannot detect a wrong premise.

RULE
Never infer a unit from the value's magnitude, and never from a field name.
`abs(x) < 1` cannot distinguish 0.5% from 50%, and four different books use
`pnl` for two different things. The unit is a property of the BOOK and is
declared here, once, next to the line number that proves it.
"""
from __future__ import annotations

# book name -> (field, unit)
#   'fraction' = already a return (0.025 = 2.5%)
#   'dollars'  = a return multiplied by position size; divide by size to recover
BOOKS: dict[str, tuple[str, str]] = {
    "futures_executor": ("pnl", "dollars"),
    "adaptive_engine": ("pnl", "dollars"),
    "unified_trader": ("pnl", "dollars"),
    "regime_trader": ("ret", "fraction"),
    "trend_follower": ("ret", "fraction"),
    "live_executor": ("ret", "fraction"),
    "bybit_demo_trader": ("ret", "fraction"),
    # The REAL book's closed records (T0.10). `pnl_usd` is DOLLARS and is only
    # present when entry, exit AND fee were all known at close (see
    # _close_real in trend_follower.py). A null here means UNKNOWN, never zero —
    # summarise_dollars counts those separately rather than averaging them in.
    "real_closed": ("pnl_usd", "dollars"),
}


def as_percent(book: str, record: dict) -> float | None:
    """Per-trade return as a PERCENT, or None if the record cannot supply one.

    Returns None rather than 0.0 when a value is missing: an unmeasurable trade
    must not be averaged in as a break-even one.
    """
    if book not in BOOKS:
        raise KeyError(
            f"unknown book {book!r} — add it to BOOKS with its field and unit. "
            f"Guessing the unit is how the -251% drawdown was invented.")
    field, unit = BOOKS[book]

    # real_closed stores a NET DOLLAR pnl (`pnl_usd`) plus the fill prices. Its
    # percent must come from the PRICES, never from pnl_usd divided by `qty`
    # (qty is a base-asset amount, not a USD notional — that division would be
    # the exact unit confusion this module exists to prevent).
    if book == "real_closed":
        entry, exit_ = record.get("entry"), record.get("exit")
        if (isinstance(entry, (int, float)) and isinstance(exit_, (int, float))
                and entry):
            return (exit_ - entry) / entry * 100
        return None

    # Prefer an explicitly stored return when the book has one. For
    # adaptive_engine, `ret_pct` supersedes the dollar `pnl` and is exact —
    # it is the value the learner itself uses.
    if book == "adaptive_engine" and record.get("ret_pct") is not None:
        v = record["ret_pct"]
        return v * 100 if isinstance(v, (int, float)) else None

    v = record.get(field)
    if not isinstance(v, (int, float)):
        return None

    if unit == "fraction":
        return v * 100

    # dollars -> percent: divide by the position size that produced them.
    #
    # The size field differs by book and the difference is not cosmetic:
    #   adaptive_engine / unified_trader  use `size` (a USD notional)
    #   futures_executor                  uses `qty` (a BASE-ASSET quantity)
    #
    # futures' pnl is (px - entry) * qty, so dividing by `size` would be wrong
    # even if `size` existed. Both are tried, and the result is validated
    # against the prices below before being trusted.
    for size_key in ("size", "qty"):
        size = record.get(size_key)
        if isinstance(size, (int, float)) and size != 0:
            got = v / size * 100
            # Cross-check against entry/exit when both are present. If the price
            # move disagrees, the dollar figure and the recorded size do not
            # belong together and neither is safe to use.
            entry, exit_ = record.get("entry"), record.get("exit")
            if (isinstance(entry, (int, float)) and isinstance(exit_, (int, float))
                    and entry):
                implied = (exit_ - entry) / entry * 100
                if abs(implied - got) > 1e-6:
                    return implied
            return got

    # No size at all. The return is still recoverable from the prices, which is
    # how adaptive_engine's 17 pre-2026-09-22 records are read. Verified on the
    # real data: pnl / r is exactly 50.0 on every one, confirming a $50 position.
    entry, exit_ = record.get("entry"), record.get("exit")
    if (isinstance(entry, (int, float)) and isinstance(exit_, (int, float))
            and entry):
        return (exit_ - entry) / entry * 100
    return None


def summarise(book: str, records: list[dict]) -> dict | None:
    """Standard performance summary for one book, in percent."""
    rets = [r for r in (as_percent(book, x) for x in records) if r is not None]
    if not rets:
        return None
    n = len(rets)
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x <= 0]
    aw = sum(wins) / len(wins) if wins else 0.0
    al = abs(sum(losses) / len(losses)) if losses else 0.0
    wr = len(wins) / n * 100
    cum = sum(rets)
    c = peak = dd = 0.0
    for r in rets:
        c += r
        peak = max(peak, c)
        dd = min(dd, c - peak)
    return {
        "n": n,
        "win_rate": wr,
        "avg_win": aw,
        "avg_loss": al,
        "payoff": aw / al if al else 0.0,
        "break_even_payoff": (100 - wr) / wr if wr else 0.0,
        "cumulative": cum,
        "max_drawdown": dd,
        "expectancy": cum / n,
    }


def summarise_dollars(book: str, records: list[dict]) -> dict:
    """Dollar summary for a DOLLARS-denominated book, UNKNOWN-tolerant.

    A record whose pnl is null is UNKNOWN, not break-even: it is counted in
    `n_total` and `n_unknown` but excluded from every money figure. This is the
    T0.10 rule — a total must never be printed without the count of trades it
    could not price, or "unknown" silently reads as "zero".

    Returns a dict always (never None): `n_known == 0` is a fact the caller
    must render as "no real closes recorded yet", not as `$0.00`.
    """
    if book not in BOOKS:
        raise KeyError(
            f"unknown book {book!r} — add it to BOOKS with its field and unit.")
    field, unit = BOOKS[book]
    if unit != "dollars":
        raise ValueError(f"{book!r} is not a dollars book (unit={unit!r})")

    known = [x for x in (r.get(field) for r in records)
             if isinstance(x, (int, float))]
    n_total = len(records)
    n_known = len(known)
    n_unknown = n_total - n_known
    wins = [x for x in known if x > 0]
    losses = [x for x in known if x <= 0]
    total = sum(known)
    return {
        "n_total": n_total,
        "n_known": n_known,
        "n_unknown": n_unknown,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "win_rate": (len(wins) / n_known * 100) if n_known else None,
        "total_usd": total if n_known else None,
        "avg_usd": (total / n_known) if n_known else None,
        "avg_win_usd": (sum(wins) / len(wins)) if wins else None,
        "avg_loss_usd": (sum(losses) / len(losses)) if losses else None,
    }
