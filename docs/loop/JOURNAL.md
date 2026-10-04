# Loop journal

Newest first. Each entry: task, need check, what was done, evidence (commands and
results), and what was learned or blocked.

## 2026-09-26 · T2.6 full-history backfill + replay plan (owner request)
- **Owner idea:** pull all history and replay it through the live scan/signal code, beginning to end.
- **Need check (sizing, measured):** 30 days of aggTrades for 15 coins = 1.5 GB (BTC 358 MB/month), so all-time trades
  would be ~60–100 GB against 79 GB free: **rejected**. 1m klines ~2 MB/month/coin, so all-time is ~2–3 GB: **taken**.
  1s klines 68 MB/month BTC, so 12 months ~4–6 GB: **taken**. BTC monthly history starts 2017-08 (07 → 404).
- **Done:** monthly-file support in `history.py` (`--monthly klines_1m --since 2017-08`). Tests RED (4 failed) → GREEN
  (40 passed). Both pulls are running in the background. Added **T3.0 replay harness** with guard rails (no look-ahead canary,
  walk-forward, a 6-month untouched holdout, variants counted as DSR trials) and made T3.1 serve live and replay through one interface.
- **Daily backfill (T2.5):** 800/900 files done, the 26 missing are the unpublished 09-25 files.

## 2026-09-26 · T2.5 history backfill (owner: "harness the old data, keep the live data")
- **Need check:** probed data.binance.vision: spot aggTrades and 1s klines are published daily (latest 2026-09-24);
  spot and futures bookTicker are **not** (404), so spread/slippage can only come from the live recorder.
- **Done:** `src/trading/stream/history.py`: plans symbol × kind × day, downloads with retries, verifies SHA-256 against the
  published `.CHECKSUM` (deletes on mismatch), marks verified files with `.ok` so reruns skip them, reports 404 as "missing".
- **Evidence:** `pytest tests/loop` → 36 passed, 1 skipped; a real smoke download verified BTCUSDT-1s-2026-09-24 and
  reported 09-25 as missing. The 30-day backfill for 15 coins is running in the background (`data/local/history-backfill.log`).
- **Live recorder status:** 11.85M msgs, 0 aggTrade gaps, 4 reconnects (each written as a gap marker).
- **Honesty:** the tests were written alongside the code, not RED-first.

## 2026-09-26 · T2.2 tick ingest implemented
- **Need check:** captured 69 **real** messages from the mirror as fixtures (no invented payloads).
- **Done:** `src/trading/stream/` (normalize, gaps, throttle, recorder, book, ws_client, run_ingest) + `tests/loop/test_stream.py`.
- **Evidence:** RED first (collection error) → `pytest tests/loop`: **30 passed, 1 skipped**; live smoke (LOOP_NETWORK=1) passed;
  **real 2-minute run: 75,868 msgs, 0 gaps, 0 reconnects**; book throttle dropped 94.6% (qty-only); ~240 KB gz per 2 min
  (≈170 MB/day).
- **Bug caught by the runtime check, not the unit tests:** `REAL_SYMS` held `"btc"` not `"btcusdt"`; the server
  silently accepts unknown stream names and sends nothing (first run: 0 msgs, 3 reconnects). Fixed + regression test
  (verified failing on the bug by temporarily restoring it). Stalls now print a hint.
- **Found:** dev PC clock is **~5.4 s ahead** of Binance server time (3 samples via `/api/v3/time`). Raw receive stamps are
  kept as designed; T2.2b adds clock-offset records so replays can correct. Adjusting Windows time is the owner's call.
- **Anti-slop:** removed a redundant `except (OSError, TimeoutError, Exception)`.

## 2026-09-26 · T2.1 tick ingest design
- **Need check:** read payload specs from the official WS docs; **measured** live load instead of guessing: 30s on the mirror,
  15 symbols × 3 streams = 540 msg/s, 82 KiB/s, about 6.75 GiB/day raw, and bookTicker is 89%. Checked disk (D: 81 GB free).
- **Done:** `docs/loop/designs/T2.1.md`. `src/trading/stream/` (ws client pinned to the market-data mirror, normalizer,
  in-memory book, hourly gzip JSONL recorder, gap detector). Keeps bookTicker updates only on price changes plus 1/s snapshots
  (the drop share is measured in T2.3). Local receive timestamps, since bookTicker has none. Reconnect at 23h. No new deps (aiohttp).
- **Anti-slop:** verified that aiohttp 3.14.3 has `autoping`; stated the measurement time in UTC; flagged the one-sample load.
- **Evidence:** design only; the measurement script output is in this iteration's log.

## 2026-09-26 · T1.5 claims vs fees
- **Need check:** repo-only. The exit rules (breakeven lock, trail, time stops) mean fixed-TP/SL p* doesn't apply,
  so I switched to a "net per trade" test; the ledger files are needed (added to E1).
- **Done:** `docs/research/claims-vs-fees.md`. **Derived from the repo's own numbers:** the 152-trade ledger behind the
  2026-09-06 aggressive shift sums to +$0.52 gross on $5 positions, and at 0.144% RT that is net-positive only under 72
  trades, so it was **net negative**. The studies behind the 70.2% capitulation and 15m SHARP claims contain **no fee
  handling**. Only regime_trader's headline states fees included. Re-test order: futures SHARP (3x real money) → regime L/S →
  capitulation → unified buckets.
- **Escalation:** E7 (should `futures_executor` keep trading 3x real money on a signal whose only evidence is gross?).
- **Anti-slop:** every figure not read from the repo is labelled **derived**, with its arithmetic shown.

## 2026-09-26 · T1.4 existing strategy inventory
- **Need check:** repo-only (docstrings, code, crontab files). Nothing executed. Live crontab unknown (E1).
- **Done:** `docs/research/strategy-inventory.md`. There are **4 real-money books**, not 1: trend_follower (spot),
  futures_executor (3x futures, every 10 min, **no enable flag**), lev_satellite_manage (3x SOL, no flag), and
  live_executor (flag-gated). The two futures scripts were missing from the owner's never-run list, so I added
  T1.4b to fix the loop's own boundary.
- **Contradictions logged:** dip-buy 70.2% vs "66% still loses" (both can be true: split vs walk-forward, BTC-only;
  re-gate before trusting either). The "ELITE 87–93%" claim is contradicted by the repo's own 3.6y OOS study
  (unfiltered negative OOS; with the EMA200 filter, SOL WR 61%), and it trades 3x real money.
  Whether regime_trader is "deployed" is unclear (SYSTEM_MAP says no; crontab.backup schedules it).
- **Anti-slop:** before calling the ELITE claim unverified, read `futures_executor.py:178-200` and found the OOS
  study, so the doc now cites the repo's own counter-evidence instead of just "no OOS".
- **Learned (process):** in T1.3 I committed before cutting the branch; caught at push time. Rule: `git checkout -b` is
  the first command of every iteration (done here).

## 2026-09-26 · T1.3 strategy audit
- **Need check:** literature claims need citations, so I ran 4 web searches (OFI, crypto momentum, deflated Sharpe,
  funding/intraday reversal). Cross-checked against T1.1 costs, T1.2 sources and SYSTEM_MAP §5.
- **Done:** `docs/research/strategy-audit.md`: 10 ideas with verdicts. TEST: intraday momentum/reversal (hourly,
  TP ≥ 1%), range breakout, regime-adaptive L/S (paper). FILTER: order-flow imbalance (moves are smaller than costs,
  so it's entry timing only). BASELINE: trend_follower, adaptive capitulation. SKIP: taker mean reversion, funding
  carry. DEFER: attention, option expiry. Gate for T3.3: Deflated Sharpe plus our 5 rules plus win rate shown beside p*.
- **Found:** the repo contradicts itself on dip-buying (the runbook says ~70% WR, +0.44% EV; SYSTEM_MAP says a 66% WR
  still loses). T1.4 must reconcile this.
- **Anti-slop:** the carry-decay claim is marked as an unverified search summary; the paper summaries are
  abstract-level and the doc says so.
- **Evidence:** docs only.

## 2026-09-25 · T1.2 source inventory
- **Need check:** everything is time- and network-sensitive, so each source was tested live from this machine
  (curl / aiohttp WS) and limits were read from the official Binance docs on GitHub; CryptoPanic terms via search.
- **Done:** `docs/research/sources.md`. **Key finding:** `api/stream/fapi.binance.com`, Bybit, OKX and Coinbase
  are unreachable from dev, but the official market-data mirrors `data-api.binance.vision` and
  `wss://data-stream.binance.vision` work (a live bookTicker was received). Phase 2 can run locally on
  mirrors that physically can't trade. RSS x4, Fear & Greed, CoinGecko, mempool.space and Kraken futures
  are reachable. CryptoPanic has gone paid-only.
- **Escalations:** E6 (CryptoPanic/Reddit/Telegram-channel reading need owner decisions; VPS reachability of fapi).
- **Anti-slop:** the WS limits are documented for `stream.binance.com`, not the mirror, and are now labelled "assumed
  same". Removed an unverified claim about REST weight limits.
- **Evidence:** the reachability table is from commands run at the time of writing (in this iteration's log).

## 2026-09-25 · T1.1 fees and break-even
- **Need check:** fees are time-sensitive, so I searched. binance.com and developers.binance.com refuse connections
  from this machine (ECONNREFUSED); used the official API docs on GitHub (primary) plus secondary fee guides.
  The docs' BNB-discount examples contradict each other (0.25 vs 0.75), so the account's real rate is UNKNOWN until E1.
- **Done:** `research/07-breakeven.py` (tested, `tests/loop/test_breakeven.py`) and `docs/research/fees-and-breakeven.md`.
  Result: symmetric scalps at TP ≤ 0.3% need 74–90%+ win rates to break even, and 0.2% is near impossible.
  Viable zone: TP ≥ ~1%, asymmetric R, or maker entries. This constrains Phase 3 (score signals net of cost)
  and Phase 4 (targets ≥ 1%, and the fill model must price non-fill risk).
- **Evidence:** `pytest tests/loop`: `10 passed`. The table is script output, not hand-typed.
- **Anti-slop:** no fee number is presented as this account's actual rate; the scenarios are labelled scenarios.

## 2026-09-25 · T0.9 test_forward uses a seeded DB
- **Need check:** none external; read `forward.advance` and `AllocationConfig`.
- **Done:** `test_wallet_cap_no_dead_entries` now uses the file's own `_seed_db` fixture and an explicit
  config ($10 wallet, $4 buys), and asserts the exact entries `[4, 4, 2]`. Before, it read the real
  `data/trading.db`: it failed off-VPS, and on an empty table every assertion passed vacuously.
- **Evidence:** `pytest tests/test_forward.py`: `4 passed`. **Mutation check:** deleting the wallet cap
  (`forward.py:75-76`) makes the test fail (`1 failed`), and the file was restored afterwards (`git status` shows only
  the test file). Full suite: `54 passed, 3 skipped, 10 errors`. **0 failures** (was 1).
- **Anti-slop:** the replaced test asserted `processed >= 0`, which is always true (test theater). The new one fails when the cap is broken.

## 2026-09-25 · T0.6 design for F4 (live_executor)
- **Need check:** read `live_executor.py` and `live_client.py`; confirmed both books use the same
  `config/live.env` account. No external facts needed.
- **Done:** `docs/loop/designs/T0.6.md`. The worst finding is not the 10% self-heal threshold: on exit,
  `manage()` sells the **entire free balance** of the coin, and its universe overlaps the live
  trend book's in the same wallet. If `LIVE_ENABLED` were restored, it would sell trend_follower's coins.
  It also invents a sell qty (`size/entry`) and exit prices (ticker `px`) while the real fill is available.
  Recommendation: **Option 0**, retire it (the owner removes the cron line). Option 1 is a fix via a shared
  `real_order_journal.py` used by both scripts.
- **Anti-slop self-review:** corrected `live_client.py` line refs (`privatePostOrder` at `:148`/`:115`),
  and noted that `place_market_*` bypasses ccxt's `create_order`, so the T0.3 `params` approach needs a
  raw `newClientOrderId` there.
- **Evidence:** docs only.

## 2026-09-25 · T0.5 design for F3 (real exits not recorded)
- **Need check:** who reads the real trend book, found by grep across `scripts/`. ccxt `fetch_my_trades` exists
  (`binance.py:8392`). No prod files needed for the design; confirming the lead needs E1.
- **Done:** `docs/loop/designs/T0.5.md`. The real book has **no closed-trade record**: a real sell
  writes nothing, a sub-minimum balance deletes the record, and `st.log` is the 53-symbol paper
  log. Yet `expectancy_vs_fee.py:101` labels that log "REAL $" and `fee_audit.py` prices it at a
  hardcoded $5 stake. Design: a `real.closed` list with fill-based values and `*_status` fields,
  UNKNOWN when missing, no deletions, trade-history reconstruction only when quantities reconcile.
- **Lead for work item 1:** the "live-record discrepancy" is plausibly real numbers being derived from
  the paper log. Added to E1 as the first thing to check.
- **New task:** T0.10 (readers stop calling the paper log real).
- **Anti-slop self-review:** verified the symbol counts by parsing the file (SYMS=53, REAL_SYMS=15) and
  corrected a line range (`fee_audit.py:97-115`).
- **Evidence:** docs only.

## 2026-09-25 · T0.4 design for F2 (stranded real position)
- **Need check:** ccxt exception classes and Binance error mapping, verified in the installed
  ccxt 4.5.73 (`InsufficientFunds` at `binance.py:2791`, `-2010` → `InvalidOrder` at `:2523`). No prod files needed.
- **Done:** `docs/loop/designs/T0.4.md`. A failed sell flips the paper flag first, so the exit is
  never retried, **the hard stop turns off** and the same coin can be bought again. Design: a
  real exit pass driven by `real.open`, `exit_pending` retries classified by exception,
  never holding a symbol twice, exits recorded from the fill price or `UNKNOWN`.
- **Anti-slop self-review:** the first draft said a blind retry after a client-side timeout
  would fail harmlessly with `InsufficientFunds`. That's false when the wallet holds other units
  of the coin (as in the DOGE incident). Replaced with a sell-side `clientOrderId` lookup
  before any retry.
- **Evidence:** docs only. The implementation is T8.1 (owner-gated).

## 2026-09-25 · T0.3 design for F1 (duplicate real buy)
- **Need check:** time-sensitive facts about Binance order IDs and ccxt behaviour, so I checked
  primary sources: [binance-spot-api-docs rest-api.md](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md)
  (`newClientOrderId` is unique only among OPEN orders; a filled one can be reused) and the
  installed ccxt 4.5.73 source. No prod files needed.
- **Done:** `docs/loop/designs/T0.3.md`. It found three paths to a duplicate buy, not one:
  (A) a kill between order and save, (B) a torn write that `load()` silently reads as an empty
  book, (C) overlapping cron runs with no lock. Design: atomic save, fail closed on a corrupt
  state file, a run lock, a write-ahead intent with a client order ID that is resolved at the
  next start, and a wallet-count cap. Entry price comes from the fill, otherwise `UNKNOWN`.
- **Anti-slop self-review:** fixed wrong ccxt line references (they pointed at
  `edit_spot_order_request`, not `create_order_request`), removed an ungrounded
  "exception escapes the loop" path (the loop catches at `:307`), removed speculation about
  Binance order retention, marked the $1 dust threshold and the VPS filesystem as owner-to-confirm,
  and added path C. The audit is recorded here instead of a separate `docs/local/audits` file, to
  keep this PR to one change.
- **Evidence:** docs only; nothing to execute. The implementation is T8.1 (owner-gated).

## 2026-09-25 · T0.2 tests/loop harness
- **Need check:** no external data or prod files; test-only code.
- **Done:** `tests/loop/conftest.py` with two autouse guards (no network unless
  `@pytest.mark.network`; importing any real-money module fails the test) and
  `test_loop_harness.py` (7 tests: interpreter is 3.12, `trading` importable, both guards fire).
- **Evidence:** `.venv/Scripts/python -m pytest tests/loop -q`: `7 passed in 0.07s`.
  Full suite with `--continue-on-collection-errors`: `1 failed, 53 passed, 3 skipped, 10 errors`;
  without this change: `1 failed, 46 passed, 3 skipped, 10 errors`. Same failure and errors,
  +7 passes. The existing suite is unaffected.
- **Learned:** a pre-existing failure, `test_forward.py::test_wallet_cap_no_dead_entries`,
  opens the real `data/trading.db`. Filed as T0.9 instead of bundling a fix.

## 2026-09-25 · T0.1 loop scaffolding
- **Need check:** no external data needed; facts come from the repo and this session's findings.
- **Done:** added LOOP (contract), ROADMAP, TASKS, ESCALATIONS, JOURNAL. Created the
  `loop/integration` branch from `origin/main` at `b0e8091`.
- **Evidence:** docs only; no code changed.
- **Known baseline:** CI is red on `main` (check-books hardcoded path; `trading.data` untracked).
  The loop's own tests go in `tests/loop/`.

## 2026-10-04 · V2-2 Jev workflow + policy engine
- **Need check:** data none. Facts: the owner's Notion note (read in full; it differs from `docs/v2/contracts.md`,
  decisions recorded in `docs/v2/STAGE1-DECISIONS.md`). `api.typesafe.ai` is blocked from this sandbox by the egress
  proxy, so the adapter (V2-3) is built against an injected client; the real wire format is escalated (E4).
- **Done:** `src/trading/v2/{ids,workflow,policy}.py`, `workflows/crypto_trading_v1.json`, 46 tests (+183 in V2-1).
- **Evidence:** `pytest tests/loop/test_v2_contracts.py tests/loop/test_v2_policy.py`: 229 passed.
  7 deliberate mutations of the policy rules each made a test fail, then were reverted.
- **Learned:** the contract validator ties tier to `adjusted_confidence`, so "demotion" had to be expressed as a
  lowered adjusted confidence plus a returned reason; the reason list is not part of Contract E (no field for it).

## 2026-10-04 · V2-3 Jev adapter + fallback
- **Need check:** the TypeSafe wire format is a fact I cannot fetch (egress blocked), so it is NOT guessed:
  the adapter takes an injected client that speaks our internal shape. Escalated as E4.
- **Done:** `src/trading/v2/{adapter,fallback}.py`, 59 tests.
- **Evidence:** `pytest tests/loop/test_v2_*.py`: all green; 11 mutations, 10 caught immediately, 1 (adapter's cap
  masked by the aggregator's) caught after adding a test that forces the aggregator to misbehave.
- **Bug found by the tests, not by reading:** a pydantic `Field(...)` constant used as a default made
  `probability` required, so every good reply fell back. The failure was safe (fallback) and visible.

## 2026-10-04 · V2-4 risk engine v2
- **Need check:** read `src/trading/risk/{guards,breakers,risk_engine}.py` and `tests/test_risk.py` in full; no data needed.
- **Done:** `src/trading/v2/risk.py` (wraps GUARDS / compute_position / breakers; adds spread, duplicate, exchange,
  kill switch, short policy), 37 tests.
- **Evidence:** `pytest tests/loop/test_v2_risk.py`: 37 passed. 16 mutations of the vetoes: 15 caught, 1 equivalent
  (the contract itself refuses an approved decision with no size, which the fail-closed handler turns into a rejection).
- **Found by tests:** size rounding to nearest overshot the exposure cap; now floors.
- **Found by reading:** latent bug in legacy `_utc_week_start` (E6), not edited.

## 2026-10-04 · V2-5 feature engine + state builder
- **Need check:** no external data; read `signals/indicators.py` and `signals/regime.py` to reuse them.
- **Done:** `src/trading/v2/{events,features,state}.py`, 29 tests.
- **Evidence:** `pytest tests/loop/test_v2_features.py`: 29 passed; 15 mutations all caught.
- **Bugs found by tests:** (1) windowing before filtering let a future/foreign bar displace a valid one; (2) a test
  reference used a different RSI window than the engine (made the window part of the definition).
- **Learned:** `classify_regime` returns "RANGING" for <60 rows without saying so: a silent default; guarded by the caller.

## 2026-10-04 · V2-6 shadow pipeline
- **Need check:** reused `trading.shadow.chain.ShadowChain` (hash chain) and `replay.context.BarStore` after reading them.
- **Done:** `src/trading/v2/shadow.py`, 24 tests, `docs/v2/STATUS.md`.
- **Evidence:** `pytest tests/` (continue-on-collection-errors): 822 passed, 6 skipped, 10 errors = baseline 444/6/10 + 378
  V2 tests. `pytest tests/loop`: 719 passed, 3 skipped. 11 real mutations of the pipeline all caught (a no-op "mutation" I
  wrote by mistake was caught as meaningless and redone). `git diff origin/main -- src/trading/risk scripts tests/test_risk.py`: empty.
- **Learned:** a late-received bar is first stopped by the stale ORDER BOOK check, not the stale snapshot check; both exist
  and both are tested separately.

## 2026-10-04 · V2-7 paper book + paper context
- **Need check:** read `replay/broker.py` (PaperAccount, CostModel). Did not reuse PaperAccount: it sizes positions itself.
- **Done:** `src/trading/v2/{paper,context}.py`, optional `paper=` on `ShadowPipeline`, 36 new tests, `buying_power` check in risk v2.
- **Evidence:** `pytest tests/` (continue-on-collection-errors): 860 passed, 6 skipped, 10 errors = 444/6/10 baseline + 416 V2.
  18 mutations of paper/risk/context: 17 caught at once; the survivor (entry-fee mis-attribution on partial close) got a
  per-trade assertion and is now caught. `git diff origin/main -- src/trading/risk scripts tests/test_risk.py`: empty.
- **Found by integration:** risk v2 approved orders that cash could not fund (100%-of-equity sizing + fees). Fixed in the V2
  wrapper only (cap to free cash / (1 + 0.2%)).
- **Bug in my own tests:** integration assertions with `if ... is not None` escape hatches; replaced with exact ones
  (queue count, fill = next bar's open, 3x already_long, gap-fill price, daily-cap blocks re-entry).

## 2026-10-04 · V2-11 evidence gate, V2-10 kill controller
- **Need check:** read `docs/loop/results/P1-2026-09-27.md` and `docs/research/strategy-inventory.md` to check the spec's
  claim that the fallback strategies are "already OOS-validated". It does not hold (E9).
- **Done:** `GatedSource`; Contract J; `killswitch.py`; `halt_scope` risk check; `PaperBook.cancel_pending`; 66 new tests.
- **Evidence:** `pytest tests/` (continue-on-collection-errors): 926 passed, 6 skipped, 10 errors = 444/6/10 + 482 V2.
  17 mutations of the kill controller and its integrations; 16 caught at once; one was an invalid mutation (indentation) that
  errored instead of testing, redone and caught. `git diff origin/main -- src/trading/risk scripts tests/test_risk.py`: empty.
- **Found by integration:** the cycle read the context before cancelling resting orders, so the audit named `duplicate_order`
  before `kill_switch`. Fixed (re-read after cancel).
- **Learned:** the spec's fallback premise was never checked against the repo's own results; checking took five minutes.

## 2026-10-04 · V2-12 whole-chain invariants, slippage protection
- **Need check:** none external.
- **Done:** `tests/loop/test_v2_invariants.py` (60 randomised seeds + coverage floors); `max_slippage_bps` / `worst_price`;
  `RiskLimits` buffer invariant.
- **Evidence:** `pytest tests/` (continue-on-collection-errors): see commit. 9 mutations of the new rules, all caught.
  `git diff origin/main -- src/trading/risk scripts tests/test_risk.py`: empty.
- **The first version of the invariant test was nearly vacuous** (4 would-executes in 60 seeds, 0 stop-outs): measured, retuned,
  and given coverage floors. Only then did it find the real bug.
- **Learned:** sizing at a reference price and filling at a later one is a risk-multiplication hazard that a fixed stop price
  makes worse; per-component tests cannot see it.

## 2026-10-04 · V2-8 Jev client (+ a blind-decision bug)
- **Need check:** the Jev API wire format is a fact that can go out of date, so it was researched, not recalled. Vendor domains are
  blocked; web search works. ~10 independent write-ups agree; recorded as UNVERIFIED with provenance (E4).
- **Done:** `typesafe_client.py`, `jev_probe.py`, workflow v2 (question text), conservative-confidence rule, 62 tests.
- **Evidence:** `pytest tests/` (continue-on-collection-errors): 1057 passed, 6 skipped, 10 errors = 444/6/10 + 613 V2. 25 mutations of
  the client / confidence / feature-values rules: all caught (one survivor, `criteria` keys unchecked, got a test; one invalid
  mutation was an AttributeError, not a real result). `git diff origin/main -- src/trading/risk scripts tests/test_risk.py`: empty.
- **Found while costing the call:** the state sent to Jev contained only a snapshot ID, none of the feature numbers; records also
  stored only the id, so decisions were not reconstructable and the id was not content-derived. Fixed everywhere.
- **Learned:** I wrote a tautological assertion (`... or True`) again while rushing; caught on review. Cost numbers come from the
  real serialized request, not an estimate of one.

## 2026-10-04 · V2-8 verified against the live Jev API
- **Need check:** the owner supplied a test key and opened network access; ran the probe, then the full pipeline, then bad requests.
- **Done:** recorded the real reply as a fixture, 6 regression tests from real behaviour, docs updated with VERIFIED facts.
- **Evidence:** pipeline run: 4/4 Jev calls ok, 0 fallbacks, 268-570 ms, jev-1.13.0, ~1,800 input tokens/call. Errors: 401 / 403 / 400 / 422
  as assumed; error body shape differs from the write-ups (status-only mapping made that harmless).
- **Learned:** my chars/4 token estimate was low by ~2x; measure usage, do not estimate it. The real risk_level answer had LOW 0.39 vs EXTREME
  0.37: argmax-only policy rules can hide probability mass on the worst case.

## 2026-10-04 · V2-9 live feed, stances, runner: first live end-to-end
- **Need check:** real data needed -> public market data via data-api.binance.vision (read-only); real Jev (test key). No production files.
- **Done:** `binance_public.py`, `stances.py`, `runner.py`, gap-safe features, live-book tolerance, 48 new tests.
- **Evidence:** `pytest tests/` (continue-on-collection-errors): 1109 passed, 6 skipped, 10 errors = 444/6/10 + 665 V2. Live: 5 min, 5 real Jev
  decisions, 0 fallbacks, ~1,800 tokens/call. 40+ mutations across the new modules; every real one caught (T2 and the long-sleep test needed
  tightening). `git diff origin/main -- src/trading/risk scripts tests/test_risk.py`: empty. The key is in no file.
- **Bugs only live data found:** history() off by one + paging; order-book timestamp after `now` rejected as look-ahead (4/4 live cycles lost).
- **Bug found by thinking about live data:** a missing minute silently shifted every return (index-based windows): now MISSING data.
- **Learned:** all-LONG stances looked like a bug; checking raw prices (BTC +18.6% over SMA200, all coins above SMA50) showed it was true.

## 2026-10-04 · V2-13 calibration, multi-symbol live run, direct feed tests
- **Need check:** the open numbers (E7/E10) are data questions: measured from ~18,000 live one-minute bars, hourly ATR and book-spread samples.
- **Done:** `calibrate.py` (12 rules, each mutation-checked), proposal doc, `--run` mode; direct tests for `binance_public.py` (it had none);
  per-symbol fresh "now" in the runner; book stamped at request-send time.
- **Evidence:** `pytest tests/` (continue-on-collection-errors): 1151 passed, 6 skipped, 10 errors = 444/6/10 + 707 V2. Live: first 3-symbol run
  9 cycles but SOL never got a snapshot; after the fix 9/9 real Jev decisions, 0 fallbacks. 24 mutations across calibrate + feed, all caught
  (the +1-bar optimisation needed a request-count assertion because the result was identical).
- **Bug only multi-symbol live data showed:** one `now` per step; later symbols' books landed "after now" (the earlier single-symbol fix hid it).
- **Mistakes in my own tests:** two wrong expectations (arithmetic); a simulated 2.5 s/call latency that was unrealistic (live is ~0.75 s).
- **Learned:** a fix for one symbol is not a fix for N; every live defect now has a test that reproduces it when the fix is reverted.
