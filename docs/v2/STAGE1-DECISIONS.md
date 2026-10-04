# V2 Stage 1 — decisions and open items

Code: `src/trading/v2/contracts.py`; tests: `tests/test_v2_contracts.py`.
Source of truth: `docs/v2/contracts.md`, amended by the owner after comparing it
with the original architecture note ("HER", Notion). Where the two differed the
owner chose the note's version for the four items below.

## Owner-approved amendments to contracts.md
1. **Actions** (Contract D): `LONG, SHORT, HOLD, EXIT, REDUCE, NO_TRADE` (was `BUY/SELL/HOLD/FLAT`).
2. **Decision shape** (D): Jev decisions also carry `probabilities` (all six actions),
   `signals` (trend_quality, entry_quality, expected_edge, timing_quality, 0-1) and
   `risk_level` (LOW/MEDIUM/HIGH/EXTREME), all required for `source=jev`; the six
   `questions` are kept. A fallback decision carries none of these.
3. **Replay fields**: `Decision.state_hash/model/workflow_version`,
   `FeatureSnapshot.feature_version`, `RiskDecision.risk_version`.
4. **Protective stop**: `RiskDecision.stop_price`, `ExecutionCommand.stop_price`;
   required to open a position, forbidden on reduce-only orders.

## Additions the spec's own rules required (small, reversible)
- `RiskDecision.risk_decision_id` — H must reference it (spec's G example had none).
- `MarketEvent.timeframe` (required for `kline_closed`) and optional `sequence`.
- `ExecutionCommand.limit_price` — required iff `type=LIMIT`.
- `Decision.fallback_strategy` — one of the four named strategies, required for fallbacks.
- `ExecutionCommand.from_approved(...)` — the only sanctioned constructor: size and
  stop come from the approved risk decision, never the proposal.

## Interpretations
- Tier thresholds are lower bounds: 0.55 -> WATCH, 0.75 -> STRATEGY_CHECK, 0.90 -> EXECUTE.
- Fallback confidence must be `< 0.90` (`FALLBACK_CONFIDENCE_CEILING`), defence in depth
  behind the policy cap; a test asserts it never exceeds the policy's execute threshold.
- `TradeProposal.side` for exit/reduce/flatten means the side of the EXISTING position.
- Entry proposals require `policy_tier == EXECUTE`; non-entry intents carry no stop/target.
- `RiskDecision` must list the seven spec checks; extra checks are allowed so the existing
  engine's guards can be added, never dropped.
- Hedge-mode mapping in H: opening BUY->LONG, SELL->SHORT; reduce-only BUY->SHORT, SELL->LONG.
- `idempotency_key` must be `<proposal_id>-v<n>`.
- Hermes `status` accepts only `proposed` (inert) until the gate ladder is specified;
  `change` keys touching leverage / kill switch / risk or loss limits / max position /
  drawdown / LIVE_ENABLED / keys / withdrawals are rejected.
- Contract C `features` is a snapshot id reference; `timestamp` stays an integer.

## Open — the spec does not cover these
- **Existing risk engine** (`src/trading/risk/`): checks it enforces that G's seven
  do not name — weekly halt, drawdown halt, exposure cap, 2% per-trade risk, 5% max stop
  distance, strategy breakers. Stage 3 must wrap it and emit them as extra checks.
  Also: it is spot-only ("no leverage in v1") and its daily halt is -5% of equity,
  versus the spec's `-$1.00` cap and `max_leverage` check; and it returns notional
  (quote currency) while G/H `size` looks like base quantity (0.00092 BTC).
- Notion note items with no contract: order/position events, execution result and
  reconciliation, halt request (`HALT_ALL/SYMBOL/STRATEGY`, `REDUCE_EXPOSURE`), Hermes
  research and promotion requests, `risk.trading_allowed`, state `exchange`/`account`.
- Policy conditions in the note (`entry_quality > 0.70`, `risk != EXTREME`) and its
  other confidence cut-offs (0.65, 0.82) — only the 0.55/0.75/0.90 ladder is modelled.
- Whether `confidence` must relate to `probabilities[action]`; sums are not enforced.
- Vocabularies for regime fields and question answers (free slugs / non-empty strings).
- Bar-close alignment for `kline_closed` (is event time the open or close boundary?).
- `src/trading/data/*.py` (referenced by the guide) is not in the repository: it is
  swallowed by the unanchored `data/` rule in `.gitignore`. Contract A's overlap with
  `store.py` could not be reviewed.

## Later amendments (see docs/loop/designs/V2-12-slippage-and-invariants.md)
- `RiskDecision.max_slippage_bps` and `ExecutionCommand.worst_price` (from the note's Risk Decision), found necessary by a
  whole-chain randomised test: without them a gap between decision and fill silently multiplies risk and can make an
  approved order unfundable.
- Contract J `halt_request` (from the note's Level 5), see V2-10.
