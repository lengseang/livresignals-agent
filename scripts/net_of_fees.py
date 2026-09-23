"""net_of_fees.py — every book's result AFTER fees, with notional taken from the
record rather than assumed.

THE MISTAKE THIS REPLACES
The first version used a flat stake per book. That is wrong for three of the
four, and wrong by a lot:

    regime_trader  sized by risk, so its notional varies 3.4x WITHIN the book
                   (96, 184, 234, 293-333 USD)
                   flat-$96 assumption -> $3.67 of fees
                   actual              -> $10.59
                   a 2.9x understatement, from one wrong constant

So notional is now derived per trade, from whatever the book records:

    adaptive_engine  pnl / ret            (pnl is dollars, ret the fraction)
    regime_trader    pnl / ret            (same, and it varies per trade)
    unified_trader   `size`
    futures_executor qty * entry

and if none of those resolve, the trade is COUNTED AS UNPRICED rather than
silently charged zero. A fee of $0 because the notional was unknown is exactly
the kind of plausible-looking wrong number this whole exercise has been about.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fees import ROUND_TRIP, ROUND_TRIP_NO_BNB, MEASURED_PER_SIDE  # noqa: E402
from book_units import as_percent  # noqa: E402

D = Path("/opt/trading-agent/data")
load = lambda n: json.loads((D / n).read_text())


def notional_of(t: dict) -> float | None:
    """Per-trade notional in USD, from the record. None if underivable."""
    # price/qty pairs first — these are unambiguous
    q, e = t.get("qty"), t.get("entry")
    if isinstance(q, (int, float)) and isinstance(e, (int, float)) and q and e:
        return q * e
    # dollars / fraction = notional, where both are present and consistent
    pnl, ret = t.get("pnl"), t.get("ret")
    if (isinstance(pnl, (int, float)) and isinstance(ret, (int, float))
            and abs(ret) > 1e-9):
        n = pnl / ret
        if 0 < n < 1e7:
            return n
    # an explicit size / notional field
    for k in ("notional", "size"):
        v = t.get(k)
        if isinstance(v, (int, float)) and v:
            return float(v)
    # last resort: recover it from prices and the dollar P&L.
    #
    #     pnl = (exit - entry) / entry * size
    #  => size = pnl * entry / (exit - entry)
    #
    # This is what the 17 adaptive_engine records need: they carry entry, exit
    # and a dollar pnl, but no size and no ret. Verified on that data — the
    # recovered size is 50.0 on every one of them, matching the $50 the engine
    # was configured with, which is the independent confirmation that the
    # formula is right rather than merely plausible.
    pnl, e2, x2 = t.get("pnl"), t.get("entry"), t.get("exit")
    if (isinstance(pnl, (int, float)) and isinstance(e2, (int, float))
            and isinstance(x2, (int, float)) and e2 and (x2 - e2)):
        n = pnl * e2 / (x2 - e2)
        if 0 < n < 1e7:
            return n
    return None


BOOKS = [
    ("unified_trader", "unified-paper.json", "closed"),
    ("adaptive_engine", "adaptive-state.json", "closed_trades"),
    ("regime_trader", "regime-state.json", "log"),
    ("futures_executor", "futures-trades.json", "closed"),
]

print("=" * 84)
print("NET-OF-FEES — notional derived per trade, never assumed")
print("=" * 84)
print(f"\n  measured round trip: {ROUND_TRIP*100:.4f}% "
      f"({MEASURED_PER_SIDE*100:.4f}% per side, BNB discount active)")
print(f"  if BNB runs out   : {ROUND_TRIP_NO_BNB*100:.4f}% "
      f"(+{(ROUND_TRIP_NO_BNB/ROUND_TRIP-1)*100:.0f}%)")

print(f"\n  {'book':<20} {'n':>5} {'priced':>7} {'gross $':>10} {'fees $':>9} "
      f"{'net $':>10} {'survives':>9}")
print("  " + "-" * 76)

for book, fname, key in BOOKS:
    d = load(fname)
    recs = d.get(key) or []
    if not recs:
        continue
    gross = fees = 0.0
    n = priced = 0
    for t in recs:
        # gross P&L in dollars: prefer `pnl`, else derive from ret
        pnl = t.get("pnl")
        if not isinstance(pnl, (int, float)):
            continue
        n += 1
        gross += pnl
        nt = notional_of(t)
        if nt is not None:
            priced += 1
            fees += nt * ROUND_TRIP
    net = gross - fees
    surv = (net / gross * 100) if gross else 0.0
    flag = "" if priced == n else f"  ⚠️ {n-priced} unpriced"
    print(f"  {book:<20} {n:>5} {priced:>7} {gross:>+10.2f} {fees:>9.2f} "
          f"{net:>+10.2f} {surv:>8.0f}%{flag}")

print()
print("=" * 84)
print("BNB FEE RUNWAY")
print("=" * 84)
try:
    import ccxt
    creds = {}
    for line in Path("/opt/trading-agent/config/live.env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            creds[k.strip()] = v.strip()
    ex = ccxt.binance({
        "apiKey": creds["LIVE_API_KEY"], "secret": creds["LIVE_SECRET_KEY"],
        "enableRateLimit": True,
        "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    from fees import bnb_status
    st = bnb_status(ex)
    print(f"\n  BNB balance : {st['bnb']:.8f} = ${st['bnb_usd']:.4f}  "
          f"(price ${st['bnb_px']:.2f})")
    print(f"  fills left  : {st['fills_left']}")
    if st["discount_active"]:
        print(f"  ✅ discount active for ~{st['fills_left']} more fills; after that")
        print(f"     fees rise {st['extra_cost_pct']:.0f}% silently")
    else:
        print(f"  🔴 BNB exhausted — paying the full {st['rate_if_exhausted']*100:.2f}%")
except Exception as e:
    print(f"\n  ⚠️ exchange unreachable: {str(e)[:80]}")

print()
print("=" * 84)
print("READING RULES")
print("=" * 84)
print("""
  * a stored `pnl` is GROSS. The scan-fee column is what it would cost to
    realise, and it is subtracted here rather than rewritten into history.
  * `survives` is the share of gross that remains. A low number means the
    headline is mostly a fee liability reported as profit.
  * notional is derived per trade because sizing varies WITHIN books —
    assuming one stake understated regime_trader's fees by 2.9x.
  * any unpriced trade is flagged, never charged zero. A fee of zero because
    the notional was unknown is a plausible-looking wrong number.
""")
