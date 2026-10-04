# V2 status: what is built, what is wired, what is not

Branches (stacked, each reviewable alone; none merged, nothing on `main`):
`v2/contracts` -> `v2/policy` -> `v2/adapter` -> `v2/risk` -> `v2/features` -> `v2/shadow` -> `v2/paper` -> `v2/fallback-source` -> `v2/kill-controller` -> `v2/invariants` -> `v2/jev-client` -> `v2/live-runner` -> `v2/calibration` -> `v2/approved-limits`.
Tests: 713 V2 tests (`tests/loop/test_v2_*.py`). Full suite on this clone: 1157 passed, 6 skipped, 10 collection errors
(the same 10 files that fail on `main` for lack of `src/trading/data/`).

```
Bars/Book --> events.kline_closed_event (A) --> features.compute_features (B) --> state.build_decision_state (C)
   --> adapter.JevAdapter.evaluate (D, fallback inside) --> policy.evaluate_policy (E) --> policy.propose_trade (F)
   --> risk.evaluate_risk (G, wraps src/trading/risk) --> [ExecutionCommand.from_approved (H): PREVIEW ONLY]
   --> shadow.ShadowPipeline logs one hash-chained record per cycle.   Hermes proposals (I) are modelled, inert.
```

## Built and tested
Contracts A-I (strict, no coercion); versioned Jev workflow; policy (demote-only); adapter with hard timeout and
labelled fallback; risk wrapper (every check listed, fail closed); feature engine (no synthetic numbers, no look-ahead);
state builder; shadow pipeline; hash-chained decision log.

## NOT done, and why (in the order they block a real shadow run)
1. **Real Jev client (V2-8): BUILT and VERIFIED live** (2026-10-04, test key): reply shape, error statuses, and a full pipeline run
   against the real API. Remaining: your price/cadence, review workflow v2's question text, then flip `active.json`.
2. **Context inputs.** `PaperContextProvider` covers position/equity/day-week/HWM from the paper book. Still to wire: a real
   exchange-health probe, the kill-switch file, `data/exit-params.json` values (all injected; unknown fails closed).
3. **Fallback source.** (Note E9: three of the four named strategies FAILED the repo's own gate; `GatedSource` now keeps them out by default.)
   Still to build: `FallbackSource` must read the stances of `trend_follower`, `donchian_ensemble`, `tsmom`,
   `ctrend` (the replay plugins exist; `trend_follower` is a real-money script and must not be imported: use its replay
   twin `trend_real`/shadow signals).
4. **Legacy comparison source.** Something must supply "what the current system would do" per cycle.
5. **Runner service + bar/book feed** (a loop over the recorder's output; adds a resident process: needs your approval,
   the host has ~2.4 GB free). Until then the pipeline is exercised by replay (`ShadowPipeline.run`).
6. ~~Paper execution~~ DONE in `v2/paper` (V2.1): `PaperBook` + `PaperContextProvider`; `ShadowPipeline(paper=book)`.
   Still needs: the owner's costs (real fee/spread/slippage), E7/E8 limits, and a runner.
7. `src/trading/data/` is still missing from git (E2). It blocks the full suite, not V2.

## Hard limits kept
No order placed or previewed outside a pure data object; `config/LIVE_ENABLED.*`, `src/trading/risk/`, `scripts/`,
the crontab, pm2 and `livr-shadow` untouched; no secrets; no network (the loop harness blocks sockets and real-money imports).

## Added since the first status (all from the owner's Notion note, which `contracts.md` had dropped)
- Contract J `halt_request` and `killswitch.KillController`: deterministic latch on exchange down / stale feed / order-rate
  runaway / unexpected position / feed corruption / abnormal slippage / daily loss; unknown inputs fail closed; human-only
  reset; Hermes can only pause new entries. Thresholds are the owner's (E10).
- `risk` gained `buying_power` (orders must be fundable from free cash) and `halt_scope` checks.
- Still unmodelled from the note: order/position events and an execution-result contract (the paper book logs its own events),
  Hermes research/promotion requests, the Jev decision tree (conditional questions), the versioned-workflow `active` symlink
  beyond the JSON pointer, and Postgres/Redis (deliberately not added: memory).

## Update: the live paper runner exists (V2-9)
`python -m trading.v2.runner --demo --symbols BTCUSDT --cadence 15 --out data/local/v2` runs the whole V2 chain on live public
data with the real Jev API against the paper book. Verified live (5 minutes, 5 real decisions, 0 fallbacks). NOT deployed; a resident
process on the VPS needs your approval. `<out>/STOP` stops it, `<out>/V2_KILL` engages the kill switch, `<out>/heartbeat.json` is the
health view. Still genuinely unbuilt: see the closing inventory in `docs/loop/ESCALATIONS.md` E11.

## Update: calibrated numbers (V2-13)
`python -m trading.v2.calibrate --symbols BTCUSDT,ETHUSDT,SOLUSDT [--run --cadence 15 --minutes N]` derives the open limits from live public
data and (with --run) starts a PAPER session with them. Proposals and evidence: `docs/v2/PROPOSED-LIMITS.md`. Awaiting your approval (E12).

## Update: limits approved (2026-10-04)
The owner approved all proposed limits for PAPER use. `python -m trading.v2.runner --approved --symbols BTCUSDT,ETHUSDT,SOLUSDT --cadence 15`
runs with them (constants in `src/trading/v2/approved.py`, pinned by tests). `--demo` placeholders remain for experiments. Still needing the
owner: VPS deployment of the runner, workflow v2 question text -> `active.json`, real-money questions (not started).
