# V2-9: public data feed, real fallback stances, and the paper runner

## What was missing
The pipeline was complete but had nothing to feed it, no real source for the fallback's stances, and no process around it. Network
access (owner opened it) made all three buildable and testable on REAL data.

## Built
- `binance_public.py`: read-only REST on `data-api.binance.vision` (public mirror; api.binance.com is geo-blocked). Only CLOSED bars
  (the open bar is dropped), `end_ms = close_time + 1`, server-clock offset measured, paging for 1000+ bars, injected HTTP, errors as
  classes. Found by a real call: `history(n)` returned n-1 (open bar dropped) and stopped at 999 (paging) - both fixed.
- `stances.py`: runs the REAL `DonchianEnsemble` plugin through a minimal replay on daily bars (target weight > 0 = LONG; a zero-weight
  exit signal on a held coin = FLAT, tested because "any signal = LONG" survived the first mutation run) and a labelled DAILY
  RECONSTRUCTION of trend_follower's documented rule (not the 4-hourly live cadence; ignores the 3-position cap). Too little history =
  UNAVAILABLE, never FLAT. tsmom / ctrend are not provided (they failed the repo's gate). The trend stance also supplies the
  "what the current system would do" comparison.
- `runner.py`: prime history -> step each minute -> pipeline -> paper book -> kill controller observation -> heartbeat file.
  Human controls: `<out>/STOP` exits, `<out>/V2_KILL` engages the kill switch. No limits are defaulted; `--demo` is REQUIRED and uses
  clearly UNVALIDATED placeholders. Without a Jev key every cycle falls back and says so. It does not start itself and is not deployed.
- features: a gap in the 1m or 1h series is now MISSING data (positions index "5 bars ago", so one lost minute silently shifted every
  return); a live order book is fetched AFTER `now` was taken, so the live runner allows a bounded fetch latency (default 5 s) while
  replays stay strict (0).

## Live result (2026-10-04, public BTCUSDT data + real Jev, paper book, 5 minutes at a 1-minute cadence)
5 real Jev decisions, 0 fallbacks, jev-1.13.0, 250-281 ms, ~1,800 input tokens/call, NO_TRADE at 0.77-0.90. One cycle was refused as stale
(priming took ~70 s so the first bar was late): the staleness rule working. The current rule said LONG (all 15 coins above their SMA50,
BTC 18.6% above its SMA200) while Jev said NO_TRADE: a real disagreement, logged.
The FIRST live attempt produced 4 `no_snapshot` cycles: the order book was stamped after `now`, my look-ahead guard rejected it. Only live
data could show that; fixed with an explicit tolerance and tests in both directions.

## Failure modes / self-review
1. The runner has run for minutes, not days: restart-with-state, memory growth, clock drift over a long run and the stance refresh at
   a real midnight are tested with a fake clock only.
2. Paper results are an upper bound (fee 0.10%, no spread/slippage modelled; real costs UNKNOWN).
3. It polls REST once a minute per symbol (3 calls/symbol/step on hour boundaries); a websocket feed would be lighter but is a resident
   connection: not added.
4. Priming takes ~1 minute for 1 symbol; N symbols multiply that (history is fetched sequentially).
5. Demo limits are placeholders. Nothing should be read into paper P&L until the owner sets E7/E10.
6. The trend stance is a reconstruction: it can disagree with the live script around the 4-hourly check and the position cap.
