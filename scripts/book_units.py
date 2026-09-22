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

THE ACTUAL CONVENTIONS, verified from each writer's own expression
------------------------------------------------------------------
Never trust the field NAME. Each was read from the code that assigns it:

    writer             field      unit      via
    -----------------  ---------  --------  ------------------------------------
    futures_executor   pnl        FRACTION  pnl written as a plain return
    adaptive_engine    pnl        DOLLARS   (px-entry)/entry * size
                       ret_pct    fraction  stored beside it (new records)
    unified_trader     pnl        DOLLARS   (px-entry) * (size/entry), size=5.0
    regime_trader      ret        FRACTION  round(move, 5)
    trend_follower     ret        FRACTION  round(ret, 5)

The trap that made this necessary: `futures_executor` uses `pnl` as a fraction
while `unified_trader` and `adaptive_engine` use `pnl` as dollars. Reading all
three with one assumption produced a reported **-251% drawdown** for the adaptive
book and **+1589% cumulative with -399% drawdown** for unified — the latter
self-evidently nonsense, since a drawdown cannot exceed the peak it falls from.

RULE
Never infer the unit from the value's magnitude. `abs(x) < 1` cannot distinguish
0.5% from 50%; a magnitude heuristic silently misreads small percentages as large
ones. The unit is a property of the BOOK, so it is declared here, once.
"""
from __future__ import annotations

# book name -> (field, unit)
# 'fraction' means the stored value is already a return (0.025 = 2.5%).
# 'dollars' means it is a return multiplied by position size and must be divided
# by that size to recover the return.
BOOKS: dict[str, tuple[str, str]] = {
    "futures_executor": ("pnl", "fraction"),
    "adaptive_engine": ("pnl", "dollars"),
    "unified_trader": ("pnl", "dollars"),
    "regime_trader": ("ret", "fraction"),
    "trend_follower": ("ret", "fraction"),
    "live_executor": ("ret", "fraction"),
    "bybit_demo_trader": ("ret", "fraction"),
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
    size = record.get("size")
    if isinstance(size, (int, float)) and size != 0:
        return v / size * 100

    # No size. The dollar figure is still convertible IF the book records both
    # prices, because the return is then recoverable directly and independently:
    #
    #     r = (exit - entry) / entry
    #
    # This is used for adaptive_engine's 17 pre-2026-09-22 records, which carry
    # neither `size` nor `ret_pct`. Deriving the return from prices is sound
    # where dividing by a missing size is not — and it cross-validates the
    # dollars-as-return-times-size reading: for those records pnl / r is exactly
    # 50.0 on every row, confirming a $50 position.
    #
    # Anything that cannot supply BOTH a size and a pair of prices is refused
    # rather than guessed at.
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
