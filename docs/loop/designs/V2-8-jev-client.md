# V2-8: the real Jev (TypeSafe System One) client

## What is and is not known
`api.typesafe.ai`, `typesafe.ai`, `docs.typesafe.ai` and every third-party docs site are blocked from the build sandbox (egress
proxy); only web SEARCH works, and its summaries are not verbatim. So the wire format below is **third-party-sourced and
UNVERIFIED**. About ten independent write-ups agree on it, and it matches the owner's own note (`POST /v1/systemone`,
`state + model + questions`, `jev-latest`):

    POST https://api.typesafe.ai/v1/systemone   Authorization: Bearer <key>
    {"state": text|JSON, "model": "jev-latest",
     "questions": {name: {"type":"choice","instructions":..,"criteria":{opt: desc}} | {"type":"noul","instructions":..}}}
    -> {"model":"jev-1.13.0","answers":{name:{"type":"choice","choice":..,"probabilities":{..},"confidence":..}
                                              |{"type":"noul","noul":0.91}},"usage":{"input_tokens":..,"output_tokens":..}}
    errors 401 / 402 (payment_required) / 422 / 429 / 529 as {"error":{"message","code"}}
Also: early access since 2026-09-15 behind a waitlist; one build (jev-1.13.0); ~70-500 ms; 1,200 req/min; input tokens
only are billed, price quoted as $0.042 .. $0.42 per million (sources disagree: there is no default price in the code).

## Choice
- `TypeSafeJevClient` implements the adapter's `JevClient`; the HTTP transport is injected (`UrllibTransport` is the only
  socket in V2 and tests never use it). Key from `TYPESAFE_API_KEY` at call time; never stored, repr'd, logged or put in a
  reason. HTTP statuses map to exception CLASSES (adapter records only the class name), never to response text: a real
  project found Jev HTTP errors echoing the API key. No retries here: the adapter's fallback is the retry.
- Our "score" questions go out as `noul` (yes/no): the score is the probability of YES. That is a faithful use of a
  calibrated yes/no probability, not a guess about a "score" scale.
- Translation is STRICT (type must match what we asked, numbers in [0,1], choice must be in its own probabilities).
  If the live shape differs, replies are rejected, the adapter falls back, and `jev_share` shows 0 in the shadow summary.
- A confidence is the MORE CONSERVATIVE of the probability of the chosen action and Jev's own `confidence`.
- Workflow v2 adds the question TEXT (instructions + criteria). v1 stays as the fixture workflow and is refused by the client.
  `active.json` still points at v1 until the probe has been run: flipping it is your decision.
- `python -m trading.v2.jev_probe` sends ONE call with a synthetic state (no trading data), saves the raw reply and reports
  whether it matches. I cannot run it (no key, no egress).

## Found while costing the call: Jev would have decided blind
`DecisionState.features` is only a snapshot ID ("by reference", per `contracts.md`). The request therefore carried
`"features": "feat_..."` and NONE of the numbers (RSI, returns, volume, order book). Also the shadow record stored only the id,
so a decision could not be reconstructed from its record (the contract's own audit rule), and the id was not derived from
content, so a recomputed snapshot with different values kept the same id and state hash. Fixed: the snapshot id is
content-derived (`snapshot_content_id` / `verify_snapshot`), the request carries the feature VALUES, the client refuses to
call Jev without them (nothing is sent, nothing is spent), and every record stores the full snapshot.

## Cost (from the real request: ~3.8 kB, ~940 input tokens)
$0.042/M: $0.00004/call; 15 symbols every 15 min = 1,440 calls/day = $0.06/day ($5 lasts ~88 days); hourly = $0.014/day.
$0.42/M: $0.0004/call; $0.57/day ($5 lasts ~9 days); hourly = $0.14/day ($5 lasts ~35 days). Cadence is a pipeline parameter.
`client.usage` counts calls/tokens/statuses; `estimated_cost_usd(price)` needs your price.

## Failure modes / self-review
1. The wire shape is wrong somewhere -> safe fallback, visible as `jev_share = 0`; the probe finds it in one call.
2. The question text IS strategy: the instructions in `crypto_trading_v2.json` are mine, unvalidated. Whether Jev's answers
   carry any edge is exactly what shadow measures; it is not assumed.
3. The questions are ~70% of the request; if the API supports prompt caching or reusable questions, cost drops. Unknown.
4. `state` as JSON object vs text is unknown; `state_as_text` flips it.
5. Third-party sites in the search results look partly auto-generated; agreement among them is weaker evidence than the docs.
