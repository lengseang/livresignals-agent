#!/usr/bin/env python3
"""Break-even math for a round trip: what a trade must win just to pay costs.

For a trade that exits at take-profit +tp or stop-loss -sl (fractions of price),
paying fee f per side and losing slip s per side to spread and slippage:

    round-trip cost      c = 2 * (f + s)
    net win              tp - c
    net loss             sl + c
    break-even win rate  p* = (sl + c) / (tp + sl)

No numbers here are measured. Fee rates are the scenarios in FEE_SCENARIOS;
the account's real rate is UNKNOWN until read from GET /api/v3/account/commission
or data/fee-audit.json (see docs/research/fees-and-breakeven.md).

Run: python research/07-breakeven.py
"""
from __future__ import annotations

FEE_SCENARIOS = {
    "VIP0 no BNB (0.100%/side)": 0.00100,
    "repo assumption (0.072%/side = 0.144% RT)": 0.00072,
}
SLIP_PER_SIDE = [0.0, 0.0002, 0.0005]          # 0, 2 bp, 5 bp per side
TP_SL = [(0.002, 0.002), (0.003, 0.003), (0.005, 0.005), (0.005, 0.0025), (0.01, 0.01), (0.02, 0.01)]


def round_trip_cost(fee: float, slip: float) -> float:
    return 2 * (fee + slip)


def breakeven_win_rate(tp: float, sl: float, cost: float) -> float:
    """Win rate at which expectancy is exactly zero after costs. >1 means impossible."""
    return (sl + cost) / (tp + sl)


def main() -> None:
    for name, fee in FEE_SCENARIOS.items():
        print(f"\n== {name}")
        print(f"{'TP/SL':>11} " + " ".join(f"slip {s * 1e4:>2.0f}bp" for s in SLIP_PER_SIDE))
        for tp, sl in TP_SL:
            cells = []
            for s in SLIP_PER_SIDE:
                p = breakeven_win_rate(tp, sl, round_trip_cost(fee, s))
                cells.append(f"{'IMPOSS.' if p >= 1 else f'{p * 100:6.1f}%':>9}")
            print(f"{tp * 100:4.2f}/{sl * 100:4.2f}% " + " ".join(cells))


if __name__ == "__main__":
    main()
