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
| T0.10 | Readers stop labelling the paper trend log as real money: `expectancy_vs_fee.py:101`, `fee_audit.py:97-115` (hardcoded $5 stake), add `real.closed` row to `book_units.py`. Design in `designs/T0.5.md` §4; its own PR. Useful before `real.closed` exists: relabel + print "no real closes recorded yet" | T0.5 | todo — **evidence measured 2026-11-05 overnight**: row prints n=206 from `log`; `real.closed` n=18, pnl_usd null 18/18, fee UNKNOWN 18/18, mean +6.19% / median −1.03%, **+0.09% t=+0.05 without NEAR**; fee actually paid ≈0.07% RT vs 0.1594% charged. Design + 5-test plan: `docs/proposals/real-book-reported-on-paper-log-2026-11-05.md` |
| T0.13 | `check-books.py` §1 (UNITS) covers 4 books and NOT `trend-state.json` — real money has no unit check, the paper book does. Add a `real.closed` row to `book_units.py` and to CASES. NOTE: `check-books.py` is on the no-touch list → owner OK first | T0.10 | blocked (owner OK needed) |
| T0.11 | **Re-escalated 2026-10-20 with evidence.** `src/trading/data/` is gitignored, so `health.py` had no baseline: the 2026-10-20 fix could not be reverted, and the pre-fix bytes were lost (reconstructed in `research/_health_prefix_snapshot.py`). Any future edit of this dir is unrecoverable. See `docs/proposals/data-health-reported-the-wrong-db-2026-10-20.md`. | owner OK | owner |
| T0.12 | Audit every `--quiet` cron job for the 2026-10-19 shape: does a non-zero rc reach the owner? The lesson wrapper was fixed; the others were never checked. Deciding what the cron should SAY on rc≠0 is an owner-facing behaviour change, so escalate the wording, ship the plumbing. **DONE 2026-11-06 (audit side).** Scheduler semantics measured from `cron/executions.db`: rc≠0 IS delivered (proof: d071de6fa4a4, 2 real failures). 20 jobs audited; **2 defects live** — `run_scan_ping.sh` (48×/day STALL monitor) and `run_trading_digest.sh` discard the rc with `exit 0`; plus `run_strategy_book.sh` never checks `strategy_compact.py`. Shipped audit tool `src/trading/ops/cron_exit_audit.py` + 11 tests, 3/3 mutations caught. **Wrapper edits owner-gated → E11.** Design: `designs/T0.12.md`. Branch pushed: `origin/loop/T0.12-quiet-cron-exit` (`948117f`); PR not auto-opened (no `gh`/token on host) — open via the GitHub "compare & pull request" link and base it on `cb89893` | T0.1 | review — audit shipped, fix blocked on E11 |
| T0.14 | **The V2 paper lanes are dead and no watcher reported it.** Both lanes stopped 2026-11-05T09:01Z (`jev_key_authenticates` HTTP 403 in preflight; pm2 restart-loop ×427). The in-process watchdog's `runner_silent` cannot fire on its own process's death; the independent gate exits 1 but `v2_gate_roll.sh` ends `exit 0` (job `eb407360fa04`: 4 completed / 0 failed); the digest quotes `jev 1575/1725` from the frozen heartbeat. Shipped read-only: `research/_v2_lane_silence_probe.py`, 6 tests (`tests/loop/test_v2_lane_silence.py`, 2/2 mutations caught), `docs/proposals/v2-lane-silent-since-2026-11-05.md`. **Not shipped:** any restart, any `config/v2.env` edit, any pm2 command, any wrapper edit. Credential + rc fix = **E13** | T0.12 | blocked (E13: key is the owner's) |
| T0.15 | **The real path declines and says nothing.** `trend_follower.py`: 53-coin paper universe, 15-coin `REAL_SYMS`. 2026-11-07 12:10 run went long `INJ, ONE, PEPE` — none real-money — and printed `long 3/53` / `real positions: 0/3` with no line saying why (0 REALBUY / 0 ERR in 3 days). Fix = a non-act `INFO` event (card block `Did not act` + `paper-only N` on the run line); `REAL_SYMS` **not** widened. 7 tests (`tests/loop/test_trend_real_symbol_scope.py`, 6/6 mutations caught), incl. a drift guard on the 3 duplicate copies of `REAL_SYMS`. Design `designs/T0.15.md`. **Widening `REAL_SYMS` at all is an owner decision — see E14** | T0.1 | review — branch `loop/T0.15-real-syms-copy-drift` (PR not auto-opened: no `gh`/token on host). **LIVE and working 2026-11-09**: every run line now carries `paper-only N: [...]` |
| T0.16 | **The 2026-11-08 STOPPED-edge fix never reached a report.** `_recent_edge_net()` windowed `[now-28d, now]`, which is structurally empty for a stopped edge (`loose` last filled 2026-10-11T23:16Z; the window opened 2026-10-12T13:03Z), so the clause returned `""`. Live `recent_edge_net` keys were **`['validated']`** — the sentence was in the JOURNAL and a fixture, never in a report. Fix = anchor each dead/stopped edge's window at its own last fill; anchored lines read `28d to last fill`. Also corrected the journal's unsourced "n=17, −0.20 in the last 28d" (that is the `self_improve` disable-gate cohort, not a window; anchored 28d is **n=1162, +4.55** — which does *not* prove the stop was right, and the comment now says so). 11 tests incl. end-to-end render, 5/5 mutations caught. `tests/loop` 414 passed; whole suite 967 passed / 1 pre-existing unrelated red | T0.1 | review — branch `loop/T0.16-dead-edge-window-anchor`, commit `a56e67f` (PR not auto-opened: no `gh`/token on host) |
| T0.21 | **The p1 win-rate line argued with the p2 verdict beside it.** Rule 2 read the WHOLE book (n=2999, 42.9%) while rule 4b judged the live edge (`validated`, n=1482, 45.1%); on 2026-11-11 the live split **is one edge**, so both paragraphs read the same 1,484 rows and printed the same advice ("tighten the entry") — twice, the copy at p1 above the line with the numbers. Second defect: the comment claimed the verdict was "drawn on GROSS", but `stats_of()` writes `win_rate_gross: None` for every live population (an edge row carries gross **dollars**, a PF, never a gross win **count**), so it judged on NET while disclaiming net. Fixed: suppress the cohort line only at `n_edges == 1` (rule 4b owns it), and make the prose say NET. **8 tests** (2 rewritten/new controls: one-edge → exactly one verdict; two-edge cohort → the line still fires). **Mutation** removing the guard → exactly the one-edge test red, naming the duplicate; restored byte-identical. Whole `tests/loop` **454 passed / 3 failed** — the known order-dependent `test_edge_symbol_concentration` reds, reproduced identically on clean `HEAD` | T0.16 | review — branch `loop/T0.21-cohort-of-one` (PR not auto-opened: no `gh`/token on host) |
| T0.20 | **The trend run log prints `INFO trend @ None` and drops the reason.** `trend_follower.py:709` prints every event as `f"  {kind} {sym} @ {px}"`; the 3 symbol-less sites (`INFO` `:653`, `ERR` `:640`/`:645`) hold the sentinel `"trend"` + `None` price, so the slash-`None` renders and `extra` — the entire notice — is never printed. Live 2026-11-11: **4× `INFO trend @ None`**, each directly under an `OPEN`, all four the T0.15 paper-only notice whose reason is only in the Telegram card. **T0.15 fixed the card and never the log line beside it.** Shipped: pure `event_log_line()` in `src/trading/ops/event_log.py` + 12 tests, 3/3 mutations caught; controls pin `OPEN`/`EXIT`/`REALBUY` and the **9 symbol-carrying `ERR`** sites byte-identical. Design `designs/T0.20.md` (records the corrected first draft, which routed by *kind* and would have broken those 9). **Call site is owner-gated → E16** | T0.15 | review — helper + tests in tree; one-line call-site swap is E16 |
| T0.17 | **The two readers disagree on lifetime net for the same edge.** `improvement_loop` reports `loose n=1261, +3.54` (multi-TF collapsed, 797 rows removed); `self_improve.stats_net_of` on the same `closed` rows reports `n=1684, +6.25` (no collapse). One of them is quoting a number for a population it did not state. The disable gate — which writes the list the real-money path honours — is the uncollapsed one. Read-only analysis; **no gate change without E-series sign-off** | T0.16 | todo |
| T0.22 | **The real book's +1.05%/trade is five trades deep, and one of them cannot be reproduced.** `expectancy_vs_fee.py` prints `trend_follower (REAL $) n=212 avg gross +1.210% net/trade +1.050% t=+1.68 win 21.7% "alive, unproven"`. `research/_trend_tail_concentration.py`: median NET **-1.324%**; delete the 5 best and mean NET is **+0.003%**; top 11 rows (5%) = 54% of all dollars won. Last 14d n=25: 3 winners +151.5% gross, other 22 rows **-83.8%**. The biggest row (NEAR +85.7%, entry 2.376) is impossible by `coin_trend()` against every other record of NEAR that week, and it is **29% of the book's gross** → **E18**. Also: the log carries no `edge` field on any row, and `trend_follower.py` never reads the disable list → **E17**. Research only; no rule, gate or order path touched | T0.21 | review — `loop/T0.22-trend-tail-probe`, design `designs/T0.22.md` |
| T0.23 | **Every reader of the trend book prints the mean and hides the tail.** `expectancy_vs_fee.py:101-110` and `per_symbol_net.py` (ALL BOOKS COMBINED) both quote the arithmetic mean of position returns as `net/trade` with no median and no delete-1 mean beside it; on the real book that number is +1.05% while the median is -1.32%. Reader-only fix: print `median NET` and `delete-1 mean` in the same row for any book whose |skew| is large, and a one-line note when delete-1 flips sign. A display change, no rule reads it. **`check-books.py` is on the no-touch list — do not put it there** | T0.22 | todo |
| T0.19 | **The ensemble book stopped opening 6.4 days ago and its new card lied about when.** Finished the uncommitted T0.19 work (dead-lane block + repeat-refusal suppression): found already-executing in the tree, and its FIRST live run printed `NO OPEN IN 24 RUNS (last open <today> (0.1d))` while `opens_total` was 205 and the newest close was 2026-11-05. Cause: `if opened or not meta["last_open_utc"]` stamped the CLOCK on the deploy run. Fixed — seed from the book's own newest `open_ms`, and *no* stamp when the book has no evidence (helper says `never recorded` rather than inventing a time). 13 tests, mutation reproduces the live defect (2 red), 4/4 controls pass. Live card now: `NO OPEN IN 59 RUNS (last open 2026-11-05T04:50Z (5.6d)) — REFUSED on geometry`. **Reporting only** — `MIN_P_MARGIN`, targets and `rr` untouched; re-sizing the target is a strategy change → E15 | T0.1 | review — uncommitted in tree (survives via `auto-backup` cron; needs a branch) |

## Phase 1: Research (docs only)
| ID | Task | Deps | Status |
|---|---|---|---|
| T1.1 | Verify current Binance spot fee tier + BNB discount and the real round-trip cost; break-even edge and win-rate table per TP/SL | T0.1 | review (#8) |
| T1.2 | Source inventory: exchange WS/REST (limits), news (RSS, CryptoPanic), sentiment (Fear & Greed), on-chain, funding/OI, public Telegram/Discord channels. Cost, limits, latency, terms | T0.1 | review (#9) |
| T1.3 | Strategy audit: microstructure/scalping literature (order-book imbalance, trade-flow, VWAP reversion, funding, breakout, mean reversion) vs our 5 rules; verdict test/skip | T1.1 | review (#10) |
| T1.4 | Inventory of existing strategies already in the repo (unified_trader, adaptive_engine, regime_trader, scalping_sweep, gold_scanner...) with their claimed evidence and where it lives | T0.1 | review (#11) |
| T1.5 | Plan to re-verify the `unified_trader.py:68` claim (5m 69% WR, +$0.37) from the ledger; list the exact files needed | T1.4 | review (#13) |
| T1.4b | Add `futures_executor.py` + `lev_satellite_manage.py` (real-money futures, no enable flag) to LOOP.md never-run/never-edit lists and to `tests/loop` BLOCKED_MODULES | T1.4 | review (#12) |
| T1.6 | Measure whether the strategy lab's 54-point grid is producing observations or one observation repeated (novelty, grid reach, frozen-half, archive cost). Read-only analysis; the schedule decision is the owner's (**E12**) | T1.1 | review — tool + 14 tests shipped, 2/2 mutations caught. **Measured 2026-11-06T18:00:** 3,274 runs / 166,119 rejects; 96.2 runs/day; **8 of 54 grid points have EVER cleared the split gate, all SOL**; BTC+ETH = 66.7% of the grid and have never produced one; 1,538 runs had a survivor and **1,538 (100%) died at the boundary gate**; last NEW point 2026-10-22T09:15; `rejected.jsonl` 27.9 MB at +0.82 MB/day. Carries no `timeframe` field, so the tool documents 1h (`strategy_lab.py:69 TIMEFRAME="1h"`) — an earlier draft mislabelled these runs "15m" |

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
