"""P-A benchmarks: plain DCA and buy-and-hold comparators."""
from __future__ import annotations

from trading.allocation.engine import AllocationConfig, run_accumulation


def plain_dca(closes: list[float], cfg: AllocationConfig | None = None) -> dict:
    """Constant 1.0x monthly buys — the passive baseline."""
    cfg = cfg or AllocationConfig()
    acc = run_accumulation(closes, cfg)
    return {"btc": acc.btc, "cash_invested": acc.cash_invested, "entries": acc.entries}


def buy_and_hold(closes: list[float], start_fraction: float = 1.0) -> dict:
    """Lump-sum at first candle; holds everything to the end."""
    if not closes:
        return {"btc": 0.0, "cash_invested": 0.0}
    price0 = closes[0]
    amount = 10_000.0 * start_fraction  # benchmark base
    btc = amount / price0
    return {"btc": btc, "cash_invested": amount, "entries": 1}


def compare_strategies(closes: list[float], cfg: AllocationConfig | None = None) -> dict:
    """Compare scaled-DCA vs plain-DCA on equal total capital (fair test).

    Scales plain DCA's base amount so total cash invested matches the scaled
    run, then compares final BTC. This isolates the *timing* benefit of
    drawdown scaling from the *amount* benefit of investing more overall.
    """
    cfg = cfg or AllocationConfig()
    scaled = run_accumulation(closes, cfg)
    plain_cfg = AllocationConfig(
        base_amount=cfg.base_amount,
        interval_days=cfg.interval_days,
        lookback_days=cfg.lookback_days,
        drawdown_threshold=cfg.drawdown_threshold,
        extension_threshold=cfg.extension_threshold,
        dip_multiplier=1.0,
        extension_multiplier=1.0,
    )
    plain = run_accumulation(closes, plain_cfg)

    final_price = closes[-1] if closes else 0.0
    return {
        "final_price": final_price,
        "scaled": {"btc": scaled.btc, "cash": scaled.cash_invested, "value": scaled.btc * final_price},
        "plain": {"btc": plain.btc, "cash": plain.cash_invested, "value": plain.btc * final_price},
        "excess_btc_pct": (scaled.btc / plain.btc - 1) * 100 if plain.btc > 0 else 0.0,
        "entries": {"scaled": scaled.entries, "plain": plain.entries},
    }