# V2-5: feature engine + state builder (+ Contract A producer)

Source: owner's note layers 2-3 ("100% deterministic... Don't ask an LLM to calculate RSI"; the state builder
"produces the exact state Jev sees... reproducibility").

## Choice
- `compute_features(symbol, bars, book, now_ms)` -> `FeatureResult(snapshot | None, missing, ...)`. Definition is
  `features-v1`, written in the module docstring; any change must bump the version.
- **No synthetic numbers** (LOOP.md rule 2): if any input is missing, short or invalid there is NO snapshot and the
  result names what is missing. Nothing is zero-filled; a flat series makes RSI "undefined", not 50.
- **No look-ahead**: bars ending after the last closed 1m bar are dropped *before* the window is taken. (The tests
  found that taking the window first lets a future or foreign bar displace a valid one; fixed with overfetch.)
- Reuses `signals.indicators` (EMA, ATR) and `signals.regime.classify_regime` rather than re-deriving them. Only RSI
  is new. The classifier silently returns RANGING under 60 rows, so the engine requires 60 hourly bars itself.
- `now_ms` is injected: output is a pure function of its inputs (200x repeat test).
- State builder refuses an incomplete or stale snapshot loudly (`UnusableSnapshot`).
- `events.kline_closed_event` makes Contract A events with a stable id per (venue, symbol, interval, end_ms).

## Failure modes / adversarial self-review
1. *Features drift silently.* `FEATURE_VERSION` rides on every snapshot; a hand-computation test pins each formula.
2. *Order-book "depth" is only top-of-book quantity* (what bookTicker gives). Named so in the definition. A real depth
   feed would be features-v2.
3. *`LIQUIDITY_THIN_SPREAD_BPS = 5.0` and the regime mapping are not validated.* Marked UNVALIDATED in the code; the
   volatility label inherits the spec's TRENDING > VOLATILE precedence, so trending-and-volatile reads "normal".
4. *RSI/EMA depend on the window start.* Window length (120 hourly bars) is part of the definition.
5. *Book newer than `now`* would be look-ahead in a replay: rejected.
6. *A tautological assertion slipped into my own test* (compared a value to itself); removed after review. 15 mutations
   of the engine, all caught.
