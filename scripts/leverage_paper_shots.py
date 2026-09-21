#!/usr/bin/env python3
"""Paper-only leveraged 'test shots' on REAL live market data.

Takes one shot per day (24h apart) for each symbol at each leverage level,
holds for up to 24h, then CLOSES the position and realizes the P&L. This gives
honest per-trade outcomes including liquidation, funding, and fees.

PAPER ONLY. No real orders. Reads live candles from /tmp/live_1h.json.
"""
from __future__ import annotations

import json
import statistics

LIVE_DATA = "/tmp/live_1h.json"
MAINTENANCE = 0.005
FEE_RATE = 0.0004          # per side; round trip = 2x
FUNDING_8H = 0.0001        # ~0.01% per 8h
BANKROLL = 1000.0
SHOT_SIZE = 100.0          # margin committed per shot (paper)
LEVERAGES = [3.0, 5.0, 15.0]
HOLD_HOURS = 24            # close each shot after 24h


def load_closes(symbol: str) -> list[float]:
    return [c[1] for c in json.load(open(LIVE_DATA))[symbol]]


def simulate(symbol: str, closes: list[float], leverage: float) -> dict:
    cash = BANKROLL
    pos_qty = 0.0
    entry_price = None
    entry_hour = None
    liq_price = None
    shots = 0
    wins = 0
    losses = 0
    liquidations = 0
    total_fees = 0.0
    total_funding = 0.0
    worst_dd = 0.0

    for i in range(1, len(closes)):
        price = closes[i]

        # Funding while a position is open (3 funding events per day, pro-rated per hour)
        if pos_qty > 0 and leverage > 1:
            cost = pos_qty * price * FUNDING_8H * 3.0 / 24.0
            total_funding += cost
            cash -= cost

        # Liquidation
        if liq_price is not None and price <= liq_price:
            liquidations += 1
            losses += 1
            pos_qty = 0.0
            entry_price = None
            entry_hour = None
            liq_price = None

        # Close after HOLD_HOURS
        if pos_qty > 0 and entry_hour is not None and (i - entry_hour) >= HOLD_HOURS:
            exit_val = pos_qty * price
            cash += exit_val
            total_fees += exit_val * FEE_RATE * 2.0
            cash -= exit_val * FEE_RATE * 2.0
            pnl = exit_val - SHOT_SIZE
            if pnl >= 0:
                wins += 1
            else:
                losses += 1
            pos_qty = 0.0
            entry_price = None
            entry_hour = None
            liq_price = None

        # Open a new shot if flat and 24h since last open (tracked via entry_hour reset)
        if pos_qty == 0 and (entry_hour is None or (i - (entry_hour or 0)) >= 24):
            # ensure we only open once per day: use a separate last_open marker
            pass

        # Entry: open a shot if flat and no recent open in last 24h
        if pos_qty == 0:
            # open at the start of each day boundary (i % 24 == 0) for determinism
            if i % 24 == 0:
                margin = SHOT_SIZE
                if margin <= cash:
                    notional = margin * leverage
                    qty = notional / price
                    pos_qty = qty
                    cash -= margin
                    entry_price = price
                    entry_hour = i
                    liq_price = price * (1.0 - 1.0 / leverage + MAINTENANCE)
                    total_fees += notional * FEE_RATE * 2.0
                    cash -= notional * FEE_RATE * 2.0
                    shots += 1

        # Track worst marked drawdown on open position
        if pos_qty > 0 and entry_price:
            dd = price / entry_price - 1.0
            worst_dd = min(worst_dd, dd)

    # Close any still-open position at last price
    if pos_qty > 0:
        exit_val = pos_qty * closes[-1]
        cash += exit_val
        pnl = exit_val - SHOT_SIZE
        if pnl >= 0:
            wins += 1
        else:
            losses += 1

    return {
        "symbol": symbol,
        "leverage": leverage,
        "shots": shots,
        "wins": wins,
        "losses": losses,
        "liquidations": liquidations,
        "fees_paid": round(total_fees, 2),
        "funding_paid": round(total_funding, 2),
        "final_value": round(cash, 2),
        "return_pct": round((cash / BANKROLL - 1) * 100, 2),
        "worst_open_dd_pct": round(worst_dd * 100, 2),
    }


def main():
    results = []
    for symbol in ["BTC/USDT", "ETH/USDT", "SOL/USDT"]:
        closes = load_closes(symbol)
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        vol = statistics.pstdev(rets)
        print(f"=== {symbol} ===  (7d hourly vol ~{vol*100:.1f}%/h)")
        for lev in LEVERAGES:
            r = simulate(symbol, closes, lev)
            results.append(r)
            liq_pct = -(1 / lev - MAINTENANCE) * 100
            print(
                f"  {lev:>4.0f}x  shots={r['shots']:>2}  W={r['wins']} L={r['losses']} "
                f"liq={r['liquidations']}  fees=${r['fees_paid']:>5}  fund=${r['funding_paid']:>5}  "
                f"final=${r['final_value']:>8}  ret={r['return_pct']:>6}%  "
                f"worst_dd={r['worst_open_dd_pct']:>6}%  (liq distance={liq_pct:.1f}%)"
            )
        print()

    json.dump(results, open("/tmp/leverage_shots.json", "w"), indent=2)
    print("saved /tmp/leverage_shots.json")


if __name__ == "__main__":
    main()
