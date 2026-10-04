# V2-6: shadow pipeline (V2.0)

Source: owner's note "V2.0 Shadow: Market -> Jev -> decisions. No orders. Collect thousands of decisions" and
`docs/v2/contracts.md` rollout ladder ("Shadow first is not optional").

## Choice
`ShadowPipeline.on_bar(bar, received_ms)` runs, once per decision cadence (default 15 min, on the 1m close):
market event -> features -> state -> Jev adapter -> policy -> proposal -> risk -> LOG. One record per cycle goes to
the existing append-only hash chain (`trading.shadow.chain.ShadowChain`, lane `v2shadow`), carrying the full
contracts at every step, every risk check, an `execution_preview` where the chain would have traded, and what the
CURRENT system would have done (`comparison`). Records are labelled shadow / `real: false`.

- **No execution path**: no exchange/order import (static test + tests/loop import guard). The preview is a pure
  Contract H object.
- **Honest outcomes**: `no_snapshot` (with exactly what is missing), `snapshot_unusable`, `ignored`, `watch`,
  `escalate`, `no_proposal`, `risk_rejected`, `would_execute`, `cycle_error` (stage + exception class). Every
  cycle is written, including the 16 of 20 in the test replay that lack 241 bars.
- **Proves it ran**: `stats`/`summary()` (cycles, outcomes, Jev share, agree/disagree), INFO log per cycle, and a
  sink failure RAISES instead of being swallowed.
- **Restart-safe**: bars replayed after a restart are skipped up to the chain's last logged event.
- Decision cadence is a parameter because Jev is billed per call.

## Failure modes / adversarial self-review
1. *Shadow quietly logs nothing.* Sink failure raises; `records` must equal `cycles` (asserted).
2. *Shadow agrees with itself.* Comparison uses an INDEPENDENT `legacy_action` from the caller; with none it says
   `no_legacy` rather than pretending. Wiring a real legacy source is open (see docs/v2/STATUS.md).
3. *Position never changes in shadow* (nothing executes), so after a would-execute LONG the next cycle proposes
   another entry. That is correct for a log, but the would-execute COUNT overstates trades. Paper execution (V2-7)
   will carry position state; for now treat shadow counts as decision counts.
4. *Provider returns stale risk inputs.* The engine cannot know; `CycleContext` is the caller's responsibility and
   the day/week/HWM fields are required, not defaulted.
5. *An 11th mutation was a no-op I had written by mistake* (`if True: pass`); re-run as a real mutation
   (preview regardless of verdict) and caught. 11 of 11 real mutations caught.
