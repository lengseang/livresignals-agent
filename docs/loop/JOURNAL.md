# Loop journal

Newest first. Each entry: task, need check, what was done, evidence (commands and
results), and what was learned or blocked.

## 2026-10-08 · T0.11 per-symbol reader splits real from paper
- **Need check:** repo-only. Read `trend-state.json` (has `log` 28 recs and
  `real.closed` 1 rec), `per_symbol_net.py`, `book_units.py`, and the T0.10
  readers/design. No external facts, no prod files.
- **Orientation:** T0.10 is in `review` (dep satisfied for picking), so T0.11 was
  the first pickable `todo`. Branch cut from the tip of the unmerged T0.10 branch
  (a stack), per LOOP §1.5.
- **Done:** `scripts/per_symbol_net.py` — SOURCES now names the paper `log`
  `trend_paper_universe` (not `trend_follower`) and reads the REAL money book from
  the nested path `real.closed`; a `_dig()` key-path reader; a new
  `_print_real_book()` that reports the real book in DOLLARS, UNKNOWN-tolerant
  (never $0.00); `real_closed` excluded from the combined percent aggregate (it is
  dollars, and mixing it into a percent verdict answers neither question);
  `book_units.BOOKS` gains `trend_paper_universe` (`ret`/fraction).
- **Evidence:** `venv/bin/python -m pytest tests/loop` → **362 passed, 3 skipped**;
  full `tests` → **532 passed, 3 skipped, 0 failed**; `compileall` clean. New:
  `tests/loop/test_per_symbol_split.py` (9). Live run of `per_symbol_net.py` now
  prints `TREND REAL MONEY BOOK` first — *"no real closes with a known PnL yet
  (1 UNKNOWN — not counted)"* — and per-book attribution in the bad-symbol list
  says `trend_paper_universe`, not `trend_follower`.
- **Learned:** the same fix as T0.10 was needed three lines away from where T0.10
  stopped. The unit defect (paper presented as real) and the naming defect (log
  called `trend_follower`) travel together; the readers were fixed one at a time
  because each has its own PR. The honesty rule now holds in this reader too: the
  real book's one close is UNKNOWN (its entry was BOOKED), and it is counted, not
  zeroed.
- **Discovered:** T0.12 — the script prints one coin under two names
  (`NEAR/USDT` vs `NEARUSDT`; regime_trader stores no slash), so the combined view
  splits a symbol across two rows.

## 2026-10-08 · T0.10 readers report real vs paper, separately
- **Need check:** repo-only. Read `trend-state.json` (has `real.closed`, 1 record), the three
  readers, and `designs/T0.5.md` §4. No external facts, no prod files.
- **Orientation:** the writer side (F3, `trend_follower` `real.closed`) is already on `main`; the
  readers never followed. That makes T0.10 the first pickable `todo` (dep T0.5 is `review`).
- **Done:** `scripts/book_units.py` (`real_closed` book, `summarise_dollars`, price-derived
  percent for the real book), `scripts/fee_audit.py` (`books_from_trend` → `trend_real_closed` +
  `trend_paper_universe`, no hardcoded $5 stake on the real path), `scripts/expectancy_vs_fee.py`
  (relabelled `log` → "trend_follower (paper universe)", new `real_closed_lines`, wrapped in a
  `main()` guard so the helper is importable).
- **Evidence:** `pytest tests/loop` → **353 passed, 3 skipped**; full `pytest tests` →
  **523 passed, 3 skipped, 0 failed**. `compileall scripts/` clean. Live run of
  `expectancy_vs_fee.py` now prints the paper universe as 🔴 (gross −2.17%/trade, t=−2.25) and a
  separate REAL MONEY BOOK line: *"no real closes with a known PnL yet (1 UNKNOWN)"*. `fee_audit.py`
  prints the same UNKNOWN note for `trend_real_closed` and −2.33%/trade for the labelled paper book.
- **New tests:** `tests/loop/test_real_vs_paper_labels.py` (12) — assert the UNKNOWN-is-not-zero
  rule, the empty-book "no real closes" text, and that `real_closed` percent comes from prices, not
  `pnl_usd/qty`.
- **Learned:** the one real close in the book has `pnl_usd: null` because its entry was `BOOKED`
  (the pre-fill candle price), so the net cannot be computed honestly. Before T0.10 the reader
  fabricated a number from the paper log instead; the honest answer is "1 UNKNOWN". The honesty
  rule (LOOP §3.2) is now enforced by tests, not just by convention.
- **Discovered:** T0.11 — `per_symbol_net.py:41` reads the same paper `log` as `trend_follower`.

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
