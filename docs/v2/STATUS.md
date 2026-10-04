# V2 status: what is built, what is wired, what is not

Branches (stacked, each reviewable alone; none merged, nothing on `main`):
`v2/contracts` -> `v2/policy` -> `v2/adapter` -> `v2/risk` -> `v2/features` -> `v2/shadow`.
Tests: 378 V2 tests (`tests/loop/test_v2_*.py`). Full suite on this clone: 822 passed, 6 skipped, 10 collection errors
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
1. **Real Jev client (V2-8).** `api.typesafe.ai` is unreachable from the build sandbox and the wire format is unknown;
   the adapter takes an injected client. Needs E4 (docs, egress, key supplied by you in the environment).
2. **Context provider.** `CycleContext` (position, equity, day/week start, high-water mark, exchange health, kill
   switch, exit params) needs a real source: the paper book, a health probe, the kill-switch file, `data/exit-params.json`.
3. **Fallback source.** `FallbackSource` must read the stances of `trend_follower`, `donchian_ensemble`, `tsmom`,
   `ctrend` (the replay plugins exist; `trend_follower` is a real-money script and must not be imported: use its replay
   twin `trend_real`/shadow signals).
4. **Legacy comparison source.** Something must supply "what the current system would do" per cycle.
5. **Runner service + bar/book feed** (a loop over the recorder's output; adds a resident process: needs your approval,
   the host has ~2.4 GB free). Until then the pipeline is exercised by replay (`ShadowPipeline.run`).
6. **Paper execution (V2-7)** into a separate paper book, which also gives shadow a position state.
7. `src/trading/data/` is still missing from git (E2). It blocks the full suite, not V2.

## Hard limits kept
No order placed or previewed outside a pure data object; `config/LIVE_ENABLED.*`, `src/trading/risk/`, `scripts/`,
the crontab, pm2 and `livr-shadow` untouched; no secrets; no network (the loop harness blocks sockets and real-money imports).
