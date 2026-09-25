# Roadmap: signal scanning, paper scalping, operator terminal

**North star:** find signals with a real edge **after fees**, prove them on paper,
broadcast them honestly, and watch everything live in an operator terminal. Real
money comes only after proof, and only by the owner's decision.

**The constraint that shapes everything:** the real account is about $15 and a
round trip costs about 0.144%. Our own research (`SYSTEM_MAP.md` §5) found
high-frequency trading and scalping lost 30–57% to fees, 1h scalping was a coin
flip, and a 66% win rate still lost. So the system is built to *measure* edge
before it multiplies trades. Volume of trades is an output of proven edge, never
a goal.

Everything below is paper or simulated unless a phase says otherwise.

## Phase 0: Foundations
Loop scaffolding, a test harness that works off the VPS, escalations for the
missing prod pieces, and designs (only) for the open live-book defects F1–F4.
**Exit:** the loop runs unattended; `tests/loop/` passes on 3.12.3.

## Phase 1: Research (no code)
Inventory of legal signal, news, sentiment, on-chain and derivatives sources, with
cost, rate limits, latency and terms. An audit of published strategies against our
5 research rules. Verified fee math: the break-even edge per trade.
**Exit:** `docs/research/` holds sourced verdicts saying which ideas are worth
testing, which to skip, and whether scalping can beat fees at all at our tier.

## Phase 2: Data
Per-second market data over exchange WebSockets (trades, top of book, 1s klines),
a local recorder, and real-time news and sentiment, all timestamped.
**Exit:** 24h+ of real recorded tick data on disk, replayable in tests.

## Phase 3: Scanner and strategy registry
A strategy plugin interface, a signal event schema, a 24/7 scanner, and an evidence
store. Many strategy variants are allowed, but every one stays paper until it
passes the validation gate (IS/OOS, fees, shared account, multiple-testing correction).
**Exit:** 3+ existing strategies ported and gated; generated variants scored honestly.

## Phase 4: Paper scalping
A fill model with real fees, spread, depth-based slippage and latency, and a
paper execution engine that cannot reach a live client. Scalping strategies are
tested on recorded ticks.
**Exit:** a report of net-of-cost expectancy out of sample per scalping
strategy, which may well be "none survive".

## Phase 5: Distribution
A signal feed API (JSON + SSE/WebSocket) labelled SIMULATED, and a Telegram
broadcaster in dry-run until the owner approves the first real send.
**Exit:** the feed is served locally; Telegram messages are rendered but not sent.

## Phase 6: Operator terminal (front end)
A read-only admin terminal in `livresignals`, cyber style with square corners:
live chart, symbol list, paper positions, strategy list with evidence, live signal
stream, latency/ping, per-strategy analytics.
**Exit:** the page runs locally against the Phase 5 feed and is screenshot-verified.

## Phase 7: Adaptation
Regime, sentiment and news inputs switch strategies on and off (paper). A weekly
re-rank from the evidence store.
**Exit:** strategy activation is traceable to its inputs in the journal.

## Phase 8: Promotion path (owner-gated)
Fixes for the live-book defects F1–F3 (designed in Phase 0, implemented only with
approval), promotion criteria from paper to real, and a real-money router behind a flag.
**Exit:** the owner's decision. The loop never enters this phase alone.
