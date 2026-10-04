# V2-3: Jev adapter + deterministic fallback

Source: owner's note layer 4 and `docs/v2/contracts.md` D.

## Problem
Every outage on this box was an external dependency failing. Jev in the hot path must not make that
failure load-bearing, and "plausible but invalid" output must not be coerced into a veto or a trade.

## Choice
- `JevAdapter.evaluate(state)` always returns a valid `Decision`, never raises. A hard timeout runs the
  client in its own thread (a hung call finishes on its own; the loop never waits); a reply that arrives
  late is still a timeout.
- The client returns OUR raw shape (`RawJevResponse`). The TypeSafe wire format is unknown here
  (`api.typesafe.ai` is blocked from this sandbox) and is its own task, V2-8 (E4). Nothing is guessed.
- Strict normalisation against the workflow: missing/extra answers, scores that are not numbers in [0,1],
  choices outside the options (its own category, `unknown_enum`), and an action distribution that does not
  cover exactly the six actions or does not sum to 1 (+-0.05) are all violations. `confidence` is the
  probability Jev assigns to its chosen action.
- Fallback = the four existing strategies' stances through a `FallbackSource`: confidence is
  votes/4 capped at 0.89, missing votes count against, ties give no action. Fallback strategies are only
  AGGREGATED here; the adapter to the real strategies is V2-6.
- Reasons carry a category plus the exception class name only (never raw text, which can hold a URL or key).
- `stats` plus INFO/WARNING logs prove the adapter ran and what it did.

## Failure modes / adversarial self-review
1. *Fallback becomes a second hot path.* Capped in three places (aggregate, adapter, contract) and again in
   policy; end-to-end test shows a unanimous fallback ends at STRATEGY_CHECK and yields no proposal.
2. *Raw exception text leaks a key into logs.* Test with secrets in exception messages.
3. *Thread pile-up if Jev hangs every call.* Each call has its own one-thread pool and is not awaited, so
   hung threads accumulate until their calls end. Bounded by the client's own socket timeout; V2-8 must set one.
   Recorded rather than solved: a circuit breaker (open after N timeouts) is a follow-up.
4. *Distribution tolerance too loose/tight.* 0.05 is a judgement, not a measurement: UNKNOWN until real
   Jev replies exist. It is the only invented number here; change it from shadow data.
5. *Fallback-only outage cannot exit positions.* Spec-consistent, exposed by a test; E5.
6. *A test passing because nothing was asserted.* 11 deliberate mutations; the one that survived (adapter's own
   cap, masked by the aggregator's) got its own test.
