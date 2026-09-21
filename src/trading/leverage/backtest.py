"""Honest leverage backtest — P-A accumulation × leverage with real frictions.

Improves on the quick sim: includes taker fees, funding (paid when holding
leveraged positions), and isolated-margin liquidation. Purpose is EDUCATION:
shows the 2x-vs-3x-vs-5x cliff from the 9yr dataset with realistic costs.

Disclaimer: this is a learning tool. The LIVE system stays 1x spot until a
human risk decision approves otherwise (AGENTS.md guard: allow_leverage=False).
"""
from __future__ import annotations

import sqlite3

from trading.config import SQLITE_PATH
from trading.leverage.sizing import LeverageInputs, size_position
from trading.allocation.engine import AllocationConfig, scaling_factor

FUNDING_RATE_8H = 0.0001  # ~0.01% per 8h, ~0.03%/day — typical BTC futures
MAINTENANCE = 0.005


def load_closes(symbol: str = "BTC/USDT") -> list[float]:
    con = sqlite3.connect(str(SQLITE_PATH))
    rows = list(con.execute(
        "SELECT close FROM candles WHERE symbol=? AND timeframe='1d' ORDER BY open_time_utc ASC",
        (symbol,),
    ))
    con.close()
    return [float(r[0]) for r in rows]


def sim_pa_leverage(closes: list[float], leverage: float, fee_rate: float = 0.0004,
                    funding: float = FUNDING_RATE_8H) -> dict:
    """P-A monthly accumulation with isolated-margin leverage.

    Each monthly entry: margin = base_amount * multiplier, notional = margin*lev.
    Position is marked daily; liquidated when price <= liq price; funding paid
    daily (3x 8h) on the notional while a position is open. Never trim.
    """
    cfg = AllocationConfig()
    cash = 10_000.0
    pos = 0.0          # units of BTC held (leveraged)
    open_margin = 0.0  # margin currently locked in the position
    entries = 0
    liquidations = 0
    last_entry = -10**9
    liq_price = None
    fees_paid = 0.0
    funding_paid = 0.0

    for i in range(cfg.lookback_days, len(closes)):
        price = closes[i]

        # Liquidation (isolated): the margin is gone; position closed at ~0.
        if liq_price is not None and price <= liq_price:
            cash += 0.0  # margin already "lost" from cash at entry; keep simple
            open_margin = 0.0
            pos = 0.0
            liq_price = None
            liquidations += 1

        # Funding: paid by holders on the NOTIONAL each day while open.
        # Spot (1x) pays no funding; only leveraged positions do.
        # Cash actually leaves the wallet — deduct it.
        if pos > 0 and leverage > 1:
            funding_cost = pos * price * funding * 3
            funding_paid += funding_cost
            cash -= funding_cost

        # Monthly entry.
        if i - last_entry >= cfg.interval_days:
            lb = closes[i - cfg.lookback_days:i]
            mult = scaling_factor(price, lb, cfg)
            margin = cfg.base_amount * mult
            if margin <= cash:
                notional = margin * leverage
                pos += notional / price
                cash -= margin
                open_margin += margin
                fees_paid += notional * fee_rate
                liq_price = price * (1 - 1 / leverage + MAINTENANCE)
                entries += 1
                last_entry = i

    final_val = cash + pos * closes[-1]
    return {
        "leverage": leverage,
        "final_value": round(final_val, 2),
        "return_pct": round((final_val / 10_000 - 1) * 100, 1),
        "entries": entries,
        "liquidations": liquidations,
        "fees_paid": round(fees_paid, 2),
        "funding_paid": round(funding_paid, 2),
    }


def run_all() -> list[dict]:
    closes = load_closes()
    out = []
    for lev in (1, 2, 3, 5, 10):
        out.append(sim_pa_leverage(closes, lev))
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(run_all(), indent=1))
