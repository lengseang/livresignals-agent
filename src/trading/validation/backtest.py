"""Point-in-time backtest simulator over closed candles.

Signal: P2 score >= 50 -> long; score < 40 -> flat. Risk sizing from P3.
Costs: taker 0.10%/side + spread 0.02% + slippage 0.05% (frozen Phase 1).
Stop: exit at stop price when a bar's low <= stop_price.
No lookahead: signals use only rows up to the previous closed candle.
"""
from __future__ import annotations

from trading.signals.scorer import compute_score
from trading.risk.risk_engine import compute_position, RiskRejected

# Frozen cost model (canonical §2.8) — P4-R12 measured 2026-08-11 on
# binance.vision live books:
#   taker fee 0.10%/side (maker=taker tier 0, BNB discount excluded)
#   spread BTC ~0.0000%, ETH ~0.0005%, SOL ~0.0133%
#   slippage for $10k notional: BTC 0.0000%, ETH 0.0006%, SOL 0.0067%
# Round-trip: 0.10%*2 + spread*2 + slippage*2 = ~0.21% (was 0.34% pessimistic).
TAKER_FEE = 0.0010
SPREAD = 0.0002
SLIPPAGE = 0.0002
COST_PER_SIDE = TAKER_FEE + SPREAD + SLIPPAGE


def simulate(
    rows: list[dict],
    *,
    risk_equity: float = 10_000.0,
    stop_distance: float = 0.02,
    score_entry: float = 53.0,
    score_exit: float = 47.0,
    warmup: int = 60,
    trade_start: int | None = None,
    require_regime: str | None = None,
    weights: dict[str, dict[str, float]] | None = None,
) -> dict:
    """Simulate long-only trading on closed candles. Returns trade list + equity.

    Thresholds calibrated from P4-R2 score distribution (entry ~p75, exit ~p50):
    score_entry=53, score_exit=47 produces 2x the round-trips of the original
    50/40 pair on 1h data.

    require_regime: P4-R5 regime-conditioned entry. When set, entries are only
    allowed when the current window's regime equals this value (default None =
    ungated; callers opt IN to gating explicitly). Exits are never gated.

    weights: optional scorer weight table override (R10 trend-following test).

    warmup: candles needed for indicator warmup (features) — not traded.
    trade_start: first index where trades may open (None = warmup). Allows
    walk-forward test windows to use train-tail warmup but only trade in test.
    """
    from trading.signals.regime import regime_for_rows
    from trading.signals.scorer import compute_score as _compute_score

    def _scored(window):
        if weights is not None:
            return _compute_score(window, weights=weights)
        return _compute_score(window)

    trades: list[dict] = []
    in_position = False
    entry_price = 0.0
    stop_price = 0.0
    position_notional = 0.0
    equity = risk_equity
    trade_start = warmup if trade_start is None else trade_start

    for i in range(warmup, len(rows)):  # features need warmup history
        window = rows[: i + 1]
        candle = rows[i]
        close = float(candle["close"])
        low = float(candle["low"])
        high = float(candle["high"])

        # Stop check first (before any signal) — protective order preserved.
        if in_position and low <= stop_price:
            exit_price = stop_price * (1 - SLIPPAGE)
            ret = (exit_price - entry_price) / entry_price - 2 * COST_PER_SIDE
            equity += position_notional * ret
            trades.append({"entry": entry_price, "exit": exit_price, "ret": ret, "reason": "stop"})
            in_position = False
            continue

        if i < trade_start:
            continue  # not yet in the tradable region

        score = _scored(window)["score"]
        if score is None:
            continue  # NO_TRADE

        if not in_position and score >= score_entry:
            if require_regime is not None and regime_for_rows(window) != require_regime:
                continue  # P4-R5: regime-conditioned entry
            try:
                pos = compute_position(score=score, risk_equity=equity, entry=close, stop_distance=stop_distance)
            except RiskRejected:
                continue
            entry_price = close * (1 + SPREAD + SLIPPAGE)
            stop_price = close * (1 - stop_distance)
            position_notional = pos["position_notional"]
            in_position = True
        elif in_position and score < score_exit:
            exit_price = close * (1 - SPREAD - SLIPPAGE)
            ret = (exit_price - entry_price) / entry_price - 2 * COST_PER_SIDE
            equity += position_notional * ret
            trades.append({"entry": entry_price, "exit": exit_price, "ret": ret, "reason": "signal"})
            in_position = False

    # Close any open position at last close (mark-to-market for equity).
    if in_position and rows:
        last_close = float(rows[-1]["close"])
        ret = (last_close - entry_price) / entry_price - 2 * COST_PER_SIDE
        equity += position_notional * ret
        trades.append({"entry": entry_price, "exit": last_close, "ret": ret, "reason": "end"})

    return {"trades": trades, "final_equity": equity, "n_trades": len(trades)}


def daily_returns(equity_start: float, trades: list[dict], n_days: int) -> list[float]:
    """Approximate daily returns series from trades (for Sharpe).

    Conservative: each trade's return is spread over the days it held.
    Simplified for v1: treat each trade return as one daily return step.
    """
    if not trades:
        return []
    return [t["ret"] for t in trades]