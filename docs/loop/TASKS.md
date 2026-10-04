# Task board

Status: `todo` · `doing` · `review (#PR)` · `done` (merged) · `blocked (reason)` · `owner` (escalated, waits for the owner)
Pick the first `todo` whose deps are `done` or `review`. Add tasks you discover at the end of their phase.

## Phase 0: Foundations
| ID | Task | Deps | Status |
|---|---|---|---|
| T0.1 | Loop scaffolding: LOOP, ROADMAP, TASKS, JOURNAL, ESCALATIONS | none | done (#1) |
| T0.2 | `tests/loop/` harness + conftest that runs without `trading.data`; one smoke test | T0.1 | review (#2) |
| T0.3 | Design only: F1 duplicate-buy on crash (`trend_follower.py:301/310`) | T0.1 | review (#3) |
| T0.4 | Design only: F2 stranded position on failed sell (`trend_follower.py:239/271`) | T0.1 | review (#4) |
| T0.5 | Design only: F3 dropped records + paper price on real exits, UNKNOWN rule | T0.1 | review (#5) |
| T0.6 | Design only: F4 `live_executor.manage()` partial-drift reconcile + no invented exit | T0.1 | review (#6) |
| T0.7 | Commit `src/trading/data/` (F5) | owner files | owner |
| T0.8 | CI fix: `check-books.py` base path from env (F6) | owner OK | owner |
| T0.9 | `tests/test_forward.py::test_wallet_cap_no_dead_entries` reads the real `data/trading.db` (test line 35); fails off-VPS with `unable to open database file`. Make it use the temp-DB fixture the same file already has (line 46). Test-only change | T0.2 | review (#7) |
| T0.10 | Readers stop labelling the paper trend log as real money: `expectancy_vs_fee.py:101`, `fee_audit.py:97-115` (hardcoded $5 stake), add `real.closed` row to `book_units.py`. Design in `designs/T0.5.md` §4; its own PR. Useful before `real.closed` exists: relabel + print "no real closes recorded yet" | T0.5 | todo |

## Phase 1: Research (docs only)
| ID | Task | Deps | Status |
|---|---|---|---|
| T1.1 | Verify current Binance spot fee tier + BNB discount and the real round-trip cost; break-even edge and win-rate table per TP/SL | T0.1 | review (#8) |
| T1.2 | Source inventory: exchange WS/REST (limits), news (RSS, CryptoPanic), sentiment (Fear & Greed), on-chain, funding/OI, public Telegram/Discord channels. Cost, limits, latency, terms | T0.1 | review (#9) |
| T1.3 | Strategy audit: microstructure/scalping literature (order-book imbalance, trade-flow, VWAP reversion, funding, breakout, mean reversion) vs our 5 rules; verdict test/skip | T1.1 | review (#10) |
| T1.4 | Inventory of existing strategies already in the repo (unified_trader, adaptive_engine, regime_trader, scalping_sweep, gold_scanner...) with their claimed evidence and where it lives | T0.1 | review (#11) |
| T1.5 | Plan to re-verify the `unified_trader.py:68` claim (5m 69% WR, +$0.37) from the ledger; list the exact files needed | T1.4 | review (#13) |
| T1.4b | Add `futures_executor.py` + `lev_satellite_manage.py` (real-money futures, no enable flag) to LOOP.md never-run/never-edit lists and to `tests/loop` BLOCKED_MODULES | T1.4 | review (#12) |

## Phase 2: Data
| ID | Task | Deps | Status |
|---|---|---|---|
| T2.1 | Design: tick ingest (bookTicker, aggTrade, kline_1s), reconnect, gap detection, storage format, retention | T1.2 | review (#14) |
| T2.2 | Implement `src/trading/stream/` ingest + recorder to `data/local/` with fixture tests | T2.1 | review (#15) |
| T2.2b | Record the local-vs-exchange clock offset (GET data-api.binance.vision/api/v3/time) as `clock` records at start + hourly, so replays can correct `ts_recv_ns`. Dev PC measured ~5.4s ahead of Binance on 2026-09-26 | T2.2 | todo |
| T2.3 | Record 24h of real ticks for the paper universe on this machine; data-quality report | T2.2 | doing (live recorder running since 2026-09-25 17:26 UTC; history backfill via T2.5) |
| T2.4 | News + sentiment ingest (RSS, free APIs), timestamped, deduped, symbol-tagged | T1.2 | todo |
| T2.5 | Backfill public daily history (aggTrades + 1s klines, 15 coins) from data.binance.vision, SHA-256 verified; spot bookTicker isn't published historically, so spreads come from the live recorder | T2.2 | review (#16) |
| T2.6 | Full-history backfill via monthly files: 1m klines since 2017-08 (all 15 coins, ~2-3 GB) + 12 months of 1s klines (~4-6 GB). Full-history aggTrades rejected: ~60-100 GB > 79 GB free | T2.5 | review (#17) |

## Phase 3: Scanner + registry
| ID | Task | Deps | Status |
|---|---|---|---|
| T3.0 | Design: **historical replay harness** (owner idea 2026-09-26): replay history bar-by-bar through the *same* scanner/strategy/paper-execution code as live, beginning to end. Guarantees: no look-ahead (strategies see only data <= t; a canary test must fail if they peek), walk-forward (fit on past window, trade the next, roll), a final holdout (last 6 months) never used for tuning, every replayed variant counted as a trial for the Deflated Sharpe gate, fills charged fees + measured spread. Output: signals + paper fills in the live ledger schema, labelled REPLAY | T2.6, T1.3 | todo |
| T3.1 | Design: strategy plugin interface, signal event schema, scanner loop, evidence store — one interface for live ticks AND replay (T3.0) | T2.1, T1.4, T3.0 | todo |
| T3.2 | Implement registry + scanner over live/replayed ticks | T3.1, T2.2 | todo |
| T3.3 | Validation gate: shared-account sim, IS/OOS, fees, BTC benchmark, deflated Sharpe | T3.1 | todo |
| T3.4 | Port 3 existing strategies as plugins (read-only copies, no edits to the originals) | T3.2 | todo |
| T3.5 | Variant generator (parameter grids) feeding the gate; report survivors | T3.3, T3.4 | todo |

## Phase 4: Paper scalping
| ID | Task | Deps | Status |
|---|---|---|---|
| T4.1 | Design: fill model (fees from T1.1, spread, depth slippage, latency) | T1.1, T2.1 | todo |
| T4.2 | Paper execution engine, with an import guard proving it cannot reach a live client | T4.1 | todo |
| T4.3 | Implement the T1.3 "test" scalping strategies; run on T2.3 data; net-of-cost OOS report | T4.2, T2.3, T3.3 | todo |

## Phase 5: Distribution
| ID | Task | Deps | Status |
|---|---|---|---|
| T5.1 | Signal feed API (FastAPI, JSON + SSE), every payload labelled SIMULATED | T3.2 | todo |
| T5.2 | Telegram broadcaster, dry-run only (renders to log); escalate for the first real send | T5.1 | todo |

## Phase 6: Operator terminal (livresignals repo)
| ID | Task | Deps | Status |
|---|---|---|---|
| T6.1 | Design: layout, data contracts, cyber square-corner style tokens | T5.1 | todo |
| T6.2 | Terminal shell page + live signal stream + latency ping (read-only) | T6.1 | todo |
| T6.3 | Live chart, symbol list, paper positions, strategy list with evidence, analytics | T6.2 | todo |

## Phase 7: Adaptation
| ID | Task | Deps | Status |
|---|---|---|---|
| T7.1 | Regime + sentiment gating of strategy activation (paper) | T3.2, T2.4 | todo |
| T7.2 | Weekly re-rank from the evidence store | T3.3 | todo |

## Phase 8: Owner-gated
| ID | Task | Deps | Status |
|---|---|---|---|
| T8.1 | Implement F1–F4 fixes from the approved designs | T0.3–T0.6 + owner | owner |
| T8.2 | Paper→real promotion criteria and a flagged real router | T4.3 + owner | owner |

## Phase V2: the Jev/Policy/Risk architecture (owner's "HER" note; spec in `docs/v2/`)
Branches `v2/*`, stacked. Shadow and paper only. Nothing here places an order or touches `LIVE_ENABLED`.
| ID | Task | Deps | Status |
|---|---|---|---|
| V2-1 | Contracts A-I as strict pure models (`src/trading/v2/contracts.py`), 183 tests | none | review (branch `v2/contracts`) |
| V2-2 | Jev workflow definition (versioned data) + policy engine + policy->TradeProposal | V2-1 | review (branch `v2/policy`) |
| V2-3 | Jev adapter with timeout/validation and the deterministic fallback (`trend_follower`, `donchian_ensemble`, `tsmom`, `ctrend`) | V2-2 | review (branch `v2/adapter`) |
| V2-4 | Risk engine v2: wraps `src/trading/risk/` (never replaces), emits Contract G with every check | V2-2 | review (branch `v2/risk`) |
| V2-5 | Feature engine (deterministic, from bars) + state builder (Contract B -> C) | V2-1 | review (branch `v2/features`) |
| V2-6 | Shadow pipeline: market -> features -> state -> adapter -> policy -> risk -> hash-chained decision log; compare with the current system. No execution | V2-3, V2-4, V2-5 | review (branch `v2/shadow`) |
| V2-7 | Paper execution of approved commands into a SEPARATE paper book (gives shadow a position state) + paper-backed context provider | V2-6 | review (branch `v2/paper`) |
| V2-10 | Deterministic kill controller + Contract J halt requests (Notion Gate 6) + `halt_scope` risk check + paper cancel | V2-7 | review (branch `v2/kill-controller`) |
| V2-11 | Evidence gate on the fallback strategies (`GatedSource`, E9) | V2-3 | review (branch `v2/fallback-source`) |
| V2-12 | Whole-chain randomised invariant test with coverage floors; gap/slippage protection (`max_slippage_bps`, `worst_price`) and the compounded-cost buffer invariant | V2-10 | review (branch `v2/invariants`) |
| V2-9 | Public data feed, real fallback stances (Donchian plugin + labelled trend reconstruction), legacy comparison source, paper runner with heartbeat/STOP/V2_KILL. NOT deployed: a resident process needs the owner's approval | V2-8 | review (branch `v2/live-runner`); verified live for 5 min |
| V2-8 | Real TypeSafe client (wire format -> internal raw shape), workflow v2 with question text, one-shot probe, feature-values fix | V2-6 | review (branch `v2/jev-client`); live shape UNVERIFIED until the owner runs the probe (E4) |

