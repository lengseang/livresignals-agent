"""fees.py — one authoritative answer to "what does a trade cost us?"

WHY THIS EXISTS
The recap used 0.1676% round trip. That number was assembled from a fee-audit
note and was wrong in BOTH directions at once:

  * it was too HIGH  — the BNB discount is already active, so the real rate is
                       0.0797% per side (measured over 33 real fills)
  * it was too LOW in the sense that mattered — it treated the rate as constant,
                       when it is conditional on holding BNB

Measured on 2026-09-22 from the account's own fill history:

    spot, VIP0                    0.1000% per side
    BNB discount  (-25%)          0.0750% per side, advertised
    actually charged              0.0797% per side  (n=33 fills, 0.0774-0.0821)

The gap between 0.0750 and 0.0797 is because fees are settled in BNB and the BNB
is valued at fill time, so the effective rate drifts with the BNB price. The
measured value is the one to use.

THE FAILURE MODE THIS GUARDS
Fees are paid from a BNB balance of about $0.54. When it empties, Binance falls
back to charging fees in the received asset at the FULL 0.1% rate. There is no
error, no warning, and no flag — the only symptom is that trades quietly become
25% more expensive. At the observed fill rate that is ~113 fills away.

So the rate is not a constant. It is a function of a balance that is draining.
"""
from __future__ import annotations

# Measured from real fills. See the module docstring for the derivation.
MEASURED_PER_SIDE = 0.000797          # 0.0797% with the BNB discount
NO_DISCOUNT_PER_SIDE = 0.0010         # 0.1000% fallback when BNB is exhausted
ADVERTISED_DISCOUNT_PER_SIDE = 0.00075


def round_trip(per_side: float = MEASURED_PER_SIDE) -> float:
    """Round-trip cost as a fraction. Two legs, since we open and close."""
    return per_side * 2


ROUND_TRIP = round_trip()             # 0.001594 = 0.1594%
ROUND_TRIP_NO_BNB = round_trip(NO_DISCOUNT_PER_SIDE)   # 0.0020 = 0.20%


def bnb_status(ex, bnb_price: float | None = None,
               avg_notional: float = 6.0, per_side: float = MEASURED_PER_SIDE):
    """How much runway the BNB fee balance has left.

    Returns a dict with the balance in USD and an estimate of how many more
    fills it can fund. This is the early-warning signal that the discount is
    about to lapse, which no existing script reports.
    """
    bal = ex.fetch_balance()
    total = bal["total"].get("BNB") or 0.0
    free = bal["free"].get("BNB") or 0.0
    px = bnb_price if bnb_price is not None else float(
        ex.fetch_ticker("BNB/USDT")["last"])
    usd = total * px
    per_fill_bnb = (avg_notional * per_side / px) if px else 0.0
    fills_left = int(free / per_fill_bnb) if per_fill_bnb else 0
    return {
        "bnb": total,
        "bnb_usd": usd,
        "bnb_px": px,
        "fills_left": fills_left,
        "discount_active": fills_left > 0,
        # the rate that will apply once the balance empties
        "rate_if_exhausted": NO_DISCOUNT_PER_SIDE,
        "extra_cost_pct": (NO_DISCOUNT_PER_SIDE / per_side - 1) * 100,
    }


def apply_cost(record: dict, size: float | None = None,
               per_side: float = MEASURED_PER_SIDE) -> float:
    """Net dollars for a trade, after round-trip fees.

    Used by the paper books, which currently record GROSS pnl and therefore
    overstate their own result. `unified_trader` reports +$16.21 gross; at the
    measured rate the fees are $11.00, so the honest figure is about $5.21.
    """
    gross = record.get("pnl")
    if not isinstance(gross, (int, float)):
        return 0.0
    notional = size if size is not None else record.get("size")
    if not isinstance(notional, (int, float)):
        # fall back to the entry notional when the book stores prices
        entry = record.get("entry")
        notional = notional or (size if size else 0.0)
        if notional == 0.0 and isinstance(entry, (int, float)):
            notional = 0.0
    fee = notional * round_trip(per_side) if notional else 0.0
    return gross - fee


if __name__ == "__main__":
    print(f"measured per side  : {MEASURED_PER_SIDE*100:.4f}%")
    print(f"round trip         : {ROUND_TRIP*100:.4f}%")
    print(f"round trip, no BNB : {ROUND_TRIP_NO_BNB*100:.4f}%")
    print()
    print("prior assumption   : 0.1676% round trip")
    print(f"  too high by      : "
          f"{(0.001676/ROUND_TRIP - 1)*100:.0f}%")
