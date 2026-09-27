# P1c · Target-weight orders in the replay broker

Status: approved by the owner 2026-09-27 (chat). Replay/paper only; nothing here places a real order.

## Why
A1 (Donchian ensemble, volatility-targeted) and A3 (risk-managed TSMOM, 50% de-risking) size positions as a
fraction of equity and resize them daily. The broker only had "open one equal slot" / "close all".

## Rule
- `Signal(action="target", weight=w)`, w clipped to [0, 1]: hold w × equity in that symbol after the fill.
- Fills at the next open like every order. The broker trades only the difference; costs (fee, spread,
  slippage, or the kline fill model) apply to the traded amount only.
- Reductions fill in the engine's "close" pass and additions in the "open" pass, so every sale at an instant
  frees cash before any purchase.
- Spot only: purchases are capped by cash, so the sum of weights can never lever the account.
- A partial sale is booked as a trade (share of cost basis and fees pro rata); weight 0 is an ordinary close.
- No rebalance band: the papers rebalance daily, so every non-zero difference trades. A band would be our
  invention and, if ever tried, a separate counted trial.
- Open/close signals are untouched, and a signal records `weight` only when it has one, so existing runs
  stay byte-identical (checked by re-running trend_real sma50 stop0.15 against its verified L2a re-run).
