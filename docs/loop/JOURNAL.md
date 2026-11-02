# Loop journal

Newest first. Each entry: task, need check, what was done, evidence (commands and
results), and what was learned or blocked.

## 2026-11-02 (overnight shift) · the 2.5% stop was rarely what closed a paper trade

- **Task:** overnight improvement shift. Verify the morning's paper-stop fix is
  live, then measure what it changed. No board task; no order path touched.
- **Need check:** repo-only; one read-only exchange balance call; no writes
  outside `docs/`.
- **Verified the morning fix is LIVE, not just committed.** `logs/unified.log`
  shows every new opener line carrying `(paper width 2.00%, RR 2.0)`, and all
  11 open positions in `data/unified-paper.json` imply 4.00%/2.00% — the fix
  reached the running cron, which is the part a green test suite does not prove.
- **Found (the substantive result).** Designed width is recoverable only from
  records whose stop was never trailed (`sl < entry`). Honest cost cohort
  (n=687, `cost > 0.0015`):
  `2.00% n=37` (all 10-10/10-11, mean −0.0291) vs `2.50% n=302` (mean −0.0513).
  **The cohorts barely overlap in time (3 and 4 trades on the shared days), so
  that comparison is era, not width — not quotable.**
  What *is* measurable is the conditional structure. Cross-tab of `worst_px`
  by exit reason in the 2.5% cohort (n=289): no trade that stayed inside −2%
  ever hit the stop (SL 0), and every trade that passed −2.5% exited SL with
  **none recovering**. So a stop-width change acts only on the 40/289 that
  breach; for the other 249 the TIME collar does the closing and the stop is
  decoration.
- **Refused to ship a threshold change, and said why in the doc.** Simulating
  other widths on that cohort makes tighter look monotonically better
  (−0.75% → +5.85 vs the book). But (a) `worst_px` is sampled at :16/:46 and is
  a **lower bound** on the adverse extreme, so the bias runs *with* the finding
  — tighter stops book more real breaches than the table shows; and (b) the
  −2.5% row does not reproduce the book's own −15.55 (gives −13.85), because
  the 40 real SL exits are proof of breaches the field under-counts. **A
  simulation that cannot reproduce its baseline has validated nothing.** Recorded
  in `docs/proposals/paper-stop-basis-2026-11-02.md` §7 as ordering-only. Note
  the direction it points (tighter) is the OPPOSITE of the live calibrator's
  own sweep (wider) — the pre-existing contradiction in `trade-lesson-loop` §2,
  still unresolved.
- **Also verified (clean):**
  candle store 10 symbols × 6 TFs = **60 series, every one fresh**
  (5m lag ≤4 slots), **0 forming rows**; futures path paused by design with a
  heartbeat; E9 ownership invariant holding (`owns_exchange_position: true` on
  exactly one arm, the other reports `symbol_owned_by_other_strategy`).
- **Found (owner-action item, not a bug):** `check-books.py` reports the live
  spot executor silent 12d. Cause confirmed against the **real exchange**, not
  a cached field: spot wallet holds **0.00 USDT free** (dust only: SUI 0.1,
  ADA/ XRP 0.0706, BNB 0.00035). `_real_can_trade()` requires `free >= 6.0`.
  The gate is working; the wallet is unfunded. No code change will make this
  path trade.
- **Learned:** "the parameter is now the studied value" and "the parameter
  change was measured" are different claims, and a fix can be entirely correct
  yet still not answer the question it appears to answer. The second claim needs
  a cohort comparison; here that comparison is era-confounded, and the honest
  output is the conditional structure (where the stop binds) rather than a
  before/after mean.

## 2026-11-02 · the paper book was trading the live book's disaster stop

- **Task:** autonomous improvement pass (cron). No board task. E9 needs no
  follow-up (verified live this morning); the board's open items are owner-gated
  (T0.7/T0.8/T0.11) or design-only. The improvement card's top findings were
  already-known (validated edge decaying, 28d PF 0.76), so this pass went after
  a defect the card cannot see: what the paper book actually *books*.
- **Need check:** repo-only, plus one dry run of the real opener against a
  scratch book. No order path, no exchange read, no real-money threshold, no
  disable list, no state file touched.
- **Found.** `data/exit-params.json` says `"scope": "live-trades.json ONLY"` and
  its `disaster_stop_pct` is 0.025 — a stop meant to be WIDE (the calibrator's
  plateau runs 2.5-5%) because it only bounds a tail. `_paper_open()` imported
  `live_executor._exit_params()` and used it as the paper book's INITIAL stop,
  with `tp = close*(1+2*stop)`. So the paper book booked **2.50% stops and
  +5.00% targets** while the comment three lines above the call cited the
  2026-09-16 study's **2% / 4%**. Evidence on the live book: all 5 open paper
  positions carry a +5.00% target (implied 2.50% stop, derivable from the state
  because `sl` has since been overwritten by the breakeven/trail lock — 735
  closed records read `sl == entry`, 594 `sl > entry`).
  Worse, it was not a one-off: `exit_calibrate.py` runs weekly on
  `live-trades.json`, so every calibration silently re-floored the paper stop.
  The OPEN line printed prices and never the width or its source, so nothing on
  the card could show it.
- **Done.** Paper owns its width: `PAPER_INITIAL_STOP_PCT = 0.020`,
  `PAPER_RR = 2.0`, `PAPER_STOP_CAP = 0.030`, and `_paper_exit_params()` returns
  stop and RR *together* so the target is always `rr x` the stop in use. The
  `live_executor` import is gone from the opener, which now prints
  `(paper width 2.00%, RR 2.0)`. `live_executor._EXIT_PARAMS_PATH` lifted to
  module scope so the "live book still gets the disaster stop" half is testable;
  its behaviour is unchanged. Lesson recorded as SYSTEM_MAP #20.
- **Evidence.** RED first: `tests/test_paper_stop_basis.py` 4 failed on HEAD
  before the fix (`_exit_params` present in the opener's call set, confirmed by
  parsing `git show HEAD:scripts/unified_trader.py`). GREEN 5 passed. Full suite
  **838 passed, 4 skipped, 0 failed** (baseline this pass 833/4/0). Both
  mutations caught (re-import the calibrator → structural test fails; return the
  live stop from the helper → value test fails). Reachability on the real
  opener: dry run books `SL 2196.6602 / TP 2331.1496` on a 2241.49 ETH entry =
  2.00% / 4.00% / RR 2.00.
- **Learned:** **a parameter is a measurement.** #9/#13/#15 fixed this for a
  number, #18 for a gate; this is the same rule for a knob, and the guard was
  already written — `scope` and `not_applicable_to` were in the file, and the
  one reader that mattered ignored them. A scope field no reader enforces is a
  comment. Corollary: the 2026-09-20 fix that pointed "both tracks" at one file
  had the right instinct (stop them drifting apart) and the wrong unit — one
  file, two meanings, one unstated. Do not report this as a P&L fix: the 2.5%
  width was never *measured* on the paper book as paper trades it, so this
  restores the studied value rather than proving a better one. The paired
  stop-width A/B (1.5/2.0/2.5/3.0, RR fixed, IS/OOS, shared account, fees) is
  now expressible and is the next measurement.
- **Not done, deliberately:** the 5 open positions keep the exits they were
  opened with (changing a stop mid-trade is an owner call and would make the
  population unanalysable); closed records are not rewritten (`sl` is not
  recoverable per record).

## 2026-11-02 · the E9 repair left a receipt that no code can write

- **Task:** autonomous improvement pass (cron). No board task. First job was the
  open verification E9's answer owed: confirm the lag closed on a NEW H4 bar.
- **Need check:** repo-only plus two read-only demo reads (public ticker; private
  demo position, `retCode 0`). No order path exercised, no parameter or disable
  list touched. The only live write is removing one hand-written field from the
  demo runner's state file (see below); the control's claim and the exchange
  position were not touched.
- **Found (1) E9 is live and correct.** 16:07Z on 11-01 was the last pre-fix
  pass (still `hold`, in_pos true). From 20:07Z (2ac046e live) the treatment
  logs `symbol_owned_by_other_strategy` **and still evaluates** (`action none`,
  bar 20:00), again at 00:07Z on the 00:00 bar. Both arms at bar 11-02T00:00,
  treatment flat, control holding. Demo exchange: ONE 1.2 ETH, avg 2241.49, stop
  2192.37, uPnL +40.49 — the control's position, not a second one.
  **The 10-31 lag bug is closed on real data.**
- **Found (2) the defect.** `state.json` carried `released_unowned_position` on
  `ETH_TURTLE_H4` — a receipt **no code writes** (0 hits for the name in
  `scripts/`; `git log --all -S` finds no commit that added it),
  **contradicting the log it describes** (0 `released_phantom_claim` rows in 379
  decision rows; the release was the 20:00 bar, close 2258.42, the receipt says
  2241.36, the 10-31 snapshot; "fixed in 4xx" names no commit), and **unbounded**
  (nothing refreshes or prunes it, so it would still say "released 2026-11-01"
  after the next release). The 11-01 JOURNAL's "post-fix replay →
  `released_phantom_claim`" was a replay against a temp state; the live repair
  was done by script and left no log row. Trading state was correct throughout —
  this is bookkeeping that the next human would have believed.
- **Done:** the release path pops the field (legacy receipts clear on the next
  release); a dry pass logs `would_release_phantom_claim` instead of a real
  event (it still writes no state, but is no longer silent); the event row
  carries `dry` and `bar`. Live receipt removed. Design + lesson:
  `docs/proposals/release-receipt-2026-11-02.md`.
- **Evidence:** RED first — `tests/loop/test_release_receipt_and_reentry.py`
  **2 failed, 2 passed** on `git show HEAD:scripts/run_crypto_trend_demo.py`;
  GREEN 4 passed. Mutation checks: dropping the `pop` re-fails the receipt test;
  hard-coding the non-dry event name re-fails the dry test. Full suite
  **833 passed, 4 skipped, 0 failed** (yesterday: 830/3). The two other tests in
  the file pin what a release must NOT break: the released arm can buy its own
  symbol after the owner exits, and that entry carries a committed `ledger_id`
  (otherwise the next pass releases it as a phantom).
- **Learned:** **a repair that exists only in a state file is a repair with no
  receipt.** The decision log is the audit surface because it is append-only and
  written by the pass that acted. Every state field must be written by shipped
  code, cleared on every transition that invalidates it, and false when a test
  reads it. And verify state against the log, not against a narrative written
  the same evening as the fix — the narrative was the only place the release
  was recorded, and it was wrong on the date, the price and the commit.

## 2026-11-01 · the pairing exemption let a treatment own a position it never bought (E9)

- **Task:** autonomous improvement pass (cron). No board task; resolving E9, the
  red test the morning shift escalated rather than repaired.
- **Need check:** repo-only; no order path exercised. The runner trades Bybit
  **demo** only and the module refuses any other endpoint at import. No trading
  parameter, threshold or disable list touched — this is an ownership/state
  invariant. The one live write is the phantom-claim repair in the demo state
  file, which was already corrupt and is a demo book. One read of the demo
  exchange position (private endpoint, demo credentials) confirmed ONE 1.2 ETH
  position, avg 2241.49, stop 2175.39.
- **Found (three defects, one of them only by replaying the real state):**
  1. `symbol_owner()` short-circuited on the paired partner, so **a pair member
     could adopt a position its partner already held**. 92db5bb exempted pairs
     to stop the control disarming the treatment — correct about DECISION, but
     it also removed the CLAIM check. The flat treatment adopted and wrote
     `ledger_id: None`; both arms then held 1.2 ETH on one exchange position.
  2. Nothing ever released a claim with no ledger id. The phantom survived two
     days across 12 `hold` logs. `hold` re-writes `in_pos` as-is, so re-logging
     the claim looked like confirmation of it.
  3. **My own first fix was wrong and the tests were green on it.** I put the
     repair after the `no_new_bar` continue, which returns before every
     ownership guard. An arm that had already seen the bar could therefore
     never be repaired — exactly the state's condition. Found by replaying the
     real corrupt state through the shipped `run_once()`, not by any test.
- **E9's own timeline was also wrong:** the re-adoption was **2026-10-31T13:07Z**
  (the last `adopted_existing_position` event, 4 minutes after 92db5bb landed),
  not "back by 2026-11-01T00:07Z". 00:07 is the first `hold` after a midnight
  gap. There was no new adoption on 11-01 at all — the escalation read a `hold`
  as a re-claim. **A state field with no event behind it cannot be dated from
  the log's last mention of the symbol.**
- **Done:** `2ac046e`. `symbol_owner` now blocks a paired partner's claim (it
  still never blocks `decide()`); a phantom claim is released when another arm
  can PROVE ownership (ledger id or explicit flag) and never sold, because a
  Sell is for the whole symbol; the repair runs ahead of the `no_new_bar`
  continue; the adoption block no longer `continue`s (it suppresses the buy for
  one pass and still decides, so `last_bar` advances — the 10-31 lag bug cannot
  return by this route). Two old tests that asserted `blocker is None` for
  exactly this case **had encoded the double claim as the contract** and were
  rewritten. Live state repaired. E8 answered in passing: the runner is
  scheduled by the Hermes cron subsystem (`bdd39cc7f4a7`, `7,37 * * * *`), not
  cron/systemd/pm2.
- **Evidence:** RED first — `tests/loop/test_paired_claim_single_owner.py` →
  **4 failed, 2 passed** on `git show HEAD:scripts/run_crypto_trend_demo.py`
  (the mutation run also re-failed `test_run_once_repairs_a_phantom_claim_on_a_
  NO_NEW_BAR_pass` when the early repair was disabled). With the fix: 7 passed.
  `tests/loop` **371 passed, 3 skipped**; full suite **830 passed, 3 skipped,
  0 failed** (morning: 821 passed / 1 failed — E9 was that failure).
  Post-fix replay of the real corrupt state: `released_phantom_claim` →
  treatment flat, control keeps 1.2 ETH, **no ETH Sell placed**, treatment still
  evaluated. Live pass at 18:07Z: state md5 changed, treatment flat, control
  holding.
- **Learned:** a test that asserts `blocker is None` can be asserting the bug.
  The 10-31 regression suite was written from the *symptom* it had just fixed
  ("the treatment must not go dark") and so pinned the opposite defect as the
  contract — and it stayed green through two days of a double claim. **When a
  fix exempts a pair from a guard, ask what else that exemption now admits.**
  And the instrument lesson repeats: *three* fixture errors in this session
  (zero-filled timestamps made the pass a no-op; the un-stubbed `instrument()`
  reached the live endpoint; unstubbed `RUNNER_DIR` would have appended fixture
  rows to the live decision log), while the defect that mattered was found by
  replaying reality. **Run the shipped function against the corrupt real state
  before believing the suite.**

## 2026-11-01 · the event blackout stood a crypto book down for US elections

- **Task:** autonomous improvement pass (cron). No board task; found while
  re-deriving today's blackout state by hand for the report.
- **Need check:** repo-only + one live fetch of the public calendar feed (GET,
  no key, no order). No trading parameter touched; the blackout fix only
  *narrows* when entries are allowed, never widens risk. The live cache was
  restored to its pre-investigation contents.
- **Found (three defects in `scripts/econ_calendar.py`, none in a test):**
  1. `risk_state()` gated on High-impact + USD for **every** release. Its only
     caller is a crypto book (BTC/ETH/SOL/…/XAUT) and the module already has
     `GOLD_MOVERS` + `is_gold_mover()`, which the blackout never consulted.
     `2026-11-03 10:16 USD Congressional Elections` (High, not a gold mover)
     opened a 30h stand-down for the whole book.
  2. this-week + next-week feeds overlap: live cache held 116 rows / 113
     unique (3 releases written twice). Dedupe now happens before the write.
  3. `_save_seed()` merged forever. Feed returned HTTP 429 today, the reader
     fell back to the seed, and the cache went **116 → 874 rows** (762
     already past, spanning 7 weeks). Seed now pruned to [-2d, +21d] on merge
     and on fallback read.
- **Done:** all three fixed in one file; 10 new tests in
  `tests/test_econ_calendar_blackout.py`, **5 red on the HEAD version**, 10
  green after. Mutation checks — dropping the gold-mover gate, dropping the
  dedupe, keying the dedupe on title only, and unpruning the seed merge each
  re-fail a named test. Live fetch after the fix: 119 events, 0 duplicates.
  Design: `docs/proposals/econ-calendar-blackout-scope-2026-11-01.md`.
  Full suite: **821 passed, 4 skipped, 1 failed** — the failure is
  `test_live_state_has_no_unowned_double_claim` on real state, unrelated to
  this file; escalated as E9 (not repaired from here).
- **Evidence:** `python -m pytest tests/test_econ_calendar_blackout.py` →
  10 passed; same file on `git checkout HEAD -- scripts/econ_calendar.py` →
  5 failed. `curl` to the feed → 429; `fetch_events(force=True)` → 119 events,
  0 dups (was 116 rows, 3 dups).
- **Learned:** a safety gate must name the POPULATION it protects. Rules #14,
  #15 and #17 were all about that for *measurements*; this was a *gate* whose
  premise ("the US calendar is busy") was true while its consequence ("this
  book must stop trading") was false. Ten minutes of manual re-derivation
  found what the monitors reported as healthy — the monitor was right about
  what it measured and wrong about what it meant.

## 2026-10-31 · the ownership guard was disarming the strategy it was protecting — and the dry run kept the receipt

- **Task:** autonomous improvement pass (cron). No board task; verification of
  `294d5ae`, the same-day fix for "two strategies claiming one exchange
  position", found while reading its result against the live decision log.
- **Need check:** repo-only plus one `--dry-run` of the runner (demo endpoint,
  no key, no real order). No config, parameter or disable list touched. The
  runner trades Bybit **demo** only.
- **Found (four defects, all observed live, none in a test):**
  1. `294d5ae` put the ownership guard at the top of the per-strategy body and
     `continue`d before `decide()`. `ETH_TURTLE_H4` logged
     `symbol_owned_by_other_strategy` 10 times from 08:07 UTC and its
     `last_bar` stalled at 04:00 while `RANDOM_ENTRY_CONTROL` reached 12:00 —
     two closed H4 bars behind and growing, because a skipped strategy never
     advances its clock and never evaluates its own exit.
  2. The blocker was the treatment's own luck baseline. The registry pairs
     `RANDOM_ENTRY_CONTROL` with `ETH_TURTLE_H4` via `match` (same symbol, same
     exits, random entries). The guard read "another strategy owns this symbol"
     and suppressed the TREATMENT whenever the control held a position —
     inverting the comparison the pair exists to make.
  3. `run_once()` refuses to advance `last_bar` when `dry` (documented: a
     rehearsal must not desync the live pass) but called `save_state(st)`
     unconditionally. One `--dry-run` wrote `ETH_TURTLE_H4` into the state file
     holding ETH at 2241.49 with `ledger_id: None` — the exact double claim
     `294d5ae` had just removed, re-created through the rehearsal path. I
     triggered it myself and repaired the state.
  4. Nothing stopped a non-owner exiting: a `close` is a market Sell for the
     whole position on the SYMBOL, so an unowned position would close the real
     owner's trade and book the P&L to the wrong arm.
- **Done:** ownership now governs ADOPTION ONLY — consulted solely on
  `exch_in and not local_in`; an owner always reaches `decide()`. New
  `match_group()` / `symbol_owner()` helpers, exempting paired arms. `save_state`
  dry-guarded. Exit path refuses without a committed `ledger_id` or
  `owns_exchange_position`, logging `close_refused_unowned`. State repaired
  (phantom claim released with its reason retained); the control's committed
  position untouched. Design + lesson:
  `docs/proposals/ownership-guard-pairs-2026-10-31.md`. Escalated E8: the
  runner's schedule is not in any crontab I can read.
- **Evidence:** RED first — 9 new tests across
  `test_ownership_guard_pairs.py` and `test_demo_runner_dry_and_exit.py`; the
  live-lag test failed on real state (2.0 bars). Mutation checks: unguarding
  `save_state` re-fails the dry-run test; dropping `paired` from `symbol_owner`
  re-fails 3 ownership tests; both restored. `pytest tests/loop` → **363
  passed, 3 skipped, 1 failed** (the live-lag test, which asserts the
  user-visible symptom and stays red until a real pass closes the gap).
  `compileall` clean. Dry run after the fix: state file md5 unchanged, and
  `ETH_TURTLE_H4` evaluates again (`hold`). Pre-existing
  `test_research_loop_coin_fragility` failure reproduces identically with these
  changes stashed.
- **Learned:** a guard placed on the *path* rather than on the *branch* it is
  about disables everything downstream of it, exits included. "Who owns this
  position?" is a question about adoption; answering it at the top of the loop
  also silently meant "do not trade, do not exit, do not advance the clock".
  And: a rehearsal that writes is not a rehearsal — the intent was documented
  in a comment one function away from the leak, which is why the comment did
  not prevent it. Check every writer on the dry path, not the one you
  remembered.

## 2026-10-24 · the three-hour-old forward lane was pricing entries off a candle that had not finished forming

- **Task:** autonomous improvement pass (cron). No board task; found while
  reading the lane created at 10:16 UTC today against the data it reads.
- **Need check:** repo-only plus one read of the public exchange to confirm what
  the fetcher returns (no key, no account, no order). No prod file, config,
  parameter or disable list touched — the lane is paper, the research loop is a
  report path.
- **Found:** `short_regime_paper.py` reads the newest `candles` row and treats it
  as closed. `ingest_all.py` (:12/:27/:42/:57) requests raw `limit=60` including
  the in-progress candle and the store is INSERT OR IGNORE on the primary key, so
  the forming 4h row is written ~15 min into its window and frozen at that
  snapshot forever. The lane's own run log proves it: it recorded
  `latest_bar_ms` = the 08:00 bar at 10:16 UTC. The gate (−3.5% vs SMA200 today,
  a ±0.04% band), the entry price, and the breakdown/cross rules all read that
  partial bar. Five of eight symbols are 19 h behind (eight bars).
- **Done:** `last_closed()` filters on each bar's own window
  (`open_time + duration <= now`) at all three read sites; one `meta.seams` mark
  records the boundary (nothing deleted — the lane had zero closed trades).
  Design + lesson: `docs/proposals/short-lane-forming-bar-2026-10-24.md`.
  SYSTEM_MAP §1 now states that a row is not a closed bar.
- **Companion, same pass:** `research_loop._change_lines` announced
  "REFUSED 'NEW BEST' sma_r=100 ... the incumbent stands" in the same message as
  "LIVE EDGE DECAYED: sma_r=150 — ABOVE its ceiling". The noise check compares
  the challenger with LIVE; when the challenger IS the incumbent the lead is 0
  and 0 is inside every band, so the branch can only ever fire wrongly. The
  cutoff against the previous best is kept and documented (a tie is not news);
  the decay branch is untouched.
- **Evidence:** RED first — `tests/test_short_lane_closed_bar.py` 5 failed, GREEN
  6 passed; mutation `return list(rows)` re-fails all 6. Research-loop tests
  5 passed, mutation `if True:` re-fails the 2 cutoff tests. Full suite
  **415 passed, 3 failed** (all three pre-existing: 2 in
  `test_data_health_coverage.py`, 1 in `test_research_loop_coin_fragility.py`,
  identical under `git stash`), `tests/loop` **355 passed, 3 skipped**. E2E:
  lane run twice, gate OPEN, 0 opened, one seam total.
- **Learned:** "closed" is a property of the calendar, not of the table. Fetcher,
  store and reader are each defensible and together manufacture a row that is
  newest, immutable and wrong. The guard is one line — assert
  `open_time + tf <= now` before pricing anything off the newest row — and it
  belongs in the reader, because the ingest writes correctly by its own contract.
  Also: a comparison between a thing and itself cannot produce evidence; `lead=0`
  passing "is 0 inside the band" is not a finding. The escalation is the ingest
  itself, which feeds the live scanner's 4h/1d gates the same way.

## 2026-10-18 · the "net" verdict counted its wins on GROSS — and the true rate told the book's only winner to stand down

- **Task:** autonomous improvement pass (cron). No board task; a defect found
  while re-reading the improvement layer against the live book after the
  2026-10-17 fix.
- **Need check:** repo-only. All evidence is the real paper book
  `data/unified-paper.json` and the live scripts. No prod file touched: the loop,
  night_tuner's stats helper and self_improve's reader are report/tuning paths;
  no order, param or disable-list change.
- **Found:** the per-edge and per-regime tables counted their wins on GROSS
  `pnl` (`if (t.get("pnl",0) or 0) > 0: wins += 1`) while the money beside them
  was NET of the measured round trip. On the real book `validated` read "47.5% WR
  ... -3.49 net ... DECAYING (net)" — its true net win rate is **43.6%**; `rsi2`
  read 42.9% gross vs **14.3%** net. The flattered rate ran the SAME way as the
  loss it was condemning. Same leak in `night_tuner._stats(net=True)` — whose
  win rate feeds a real-money `LIVE_STOP_PCT` adjustment — and `self_improve
  .stats_net_of` reported no gross rate at all. Fourth instance of one class
  (basis → disable predicate → demote rule → the win COUNT).
- **Consequence found while validating:** with a true net WR, edge `loose`
  (**+4.47 net — the only net-POSITIVE edge in the book**) sits at 44.1% and the
  `WR<45` floor told the owner to "stand it down". The floor is a heuristic
  about fee drag; a large sample that pays after the fee refutes it. A
  net-positive low-WR edge now gets an advisory p3 note ("preventive only, keep
  measuring"); a net-NEGATIVE edge at any WR keeps the actionable p2 verdict.
  Promote bar (WR>=58) untouched.
- **Done:** `win_rate` is net everywhere the P&L is net; `win_rate_gross` is
  carried under an honest name; `self_improve.stats_net_of` gained `wr_gross`.
  Design + lesson: `docs/proposals/win-rate-net-units-2026-10-18.md`.
- **Evidence:** RED first — `tests/test_win_rate_net_units.py` 5 failed / 1
  passed. GREEN after the fix: 7 passed. Mutation: restoring the gross count
  re-fails exactly the per-edge + night_tuner tests; files restored. Full suite
  `pytest tests/` → **638 passed, 3 skipped**. E2E on the real book:
  `improvement_loop.py` regenerated the report with `win_rate` + `win_rate_gross`
  on every row; `night_tuner._stats` on the last 100 paper trades gives gross-WR
  46.0% vs net-WR 37.0% / -1.006 net.
- **Learned:** fix the *basis* and you have fixed one figure — every number
  derived from it is still in the old units; the check is per-verdict, not
  per-variable. And: when a measurement is corrected, every threshold downstream
  of it must be re-derived. The 45% floor had only ever been fed a gross rate;
  fed the true rate it condemned the only edge that pays.

## 2026-10-17 · the per-edge demote rule was blind to NET P&L — the book's largest loss drew no verdict

- **Task:** autonomous improvement pass (cron). No board task; a defect found
  while reading the improvement layer against the live book.
- **Need check:** repo-only. All evidence is the real paper book
  `data/unified-paper.json` and the live `scripts/improvement_loop.py`. No prod
  file touched (the loop is paper/report-only).
- **Found:** `improvement_loop._suggest`'s per-edge block carried a PROMOTE rule
  (`WR>=58 & avg>0`) and a DEMOTE rule (`WR<45`). Both key on the win rate, so
  45-58% is a dead zone. On the real book edge `validated` sits at 48.4% WR and
  loses **-$2.81 net over 843 trades** — the LARGEST single loss in the book,
  bigger than every edge the rule DID flag (supertrend -1.85, pullback -1.11,
  bollinger -1.04, ema-cross -0.83, breakout -0.44) — and it drew NO suggestion.
  Win rate and P&L are different questions: an edge can win half its trades and
  still bleed if wins are small and losses large, which is what a dip opener
  does when the dip keeps going. Same class as the 2026-10-11 (gross→net basis)
  and 2026-10-13 (self_improve disable gate) fixes — but those swapped the
  *units* while this predicate stayed keyed on the win rate.
- **Done:** the demote verdict now judges NET alongside WR
  (`elif e["win_rate"] < 45 or losing:`), with the promote bar deliberately NOT
  relaxed (`and not losing` additionally stops a net-losing edge being called
  PROVEN). A net-negative edge above the WR floor now says WHY it was caught
  (the idea string carries "net-NEGATIVE despite a 48.4% win rate"). Design +
  lesson: `docs/proposals/edge-decay-net-blindspot-2026-10-17.md`.
- **Evidence:** RED first — `tests/test_improvement_edge_net_blindspot.py`
  2 failed / 2 passed (the 48.4%-WR net-loser drew no verdict; a 62%-WR
  net-loser was not flagged). GREEN after the fix: 4 passed. Mutation check:
  restoring `elif e["win_rate"] < 45:` re-fails exactly those 2; file restored.
  Full suite `pytest tests/` → **625 passed, 3 skipped**. E2E on the real book:
  suggestions now include `p2 edge_decay: Edge 'validated' DECAYING (net): 48.4%
  WR / 843 trades / -2.81 net (gross +3.43) — net-NEGATIVE despite a 48.4% win
  rate`; `loose` (+4.38 net) is correctly left alone.
- **Learned:** a verdict must be keyed on the quantity the decision is about.
  "Is this edge declining?" is a P&L question; a win-rate threshold is a proxy
  that fails exactly when the strategy has any payout asymmetry. Same shape as
  the 2026-10-16 single-valued-regime lesson: test the *condition*, not the
  *label* that happened to expose it. And: fixing the *basis* (gross→net) is not
  the same as fixing the *predicate* — check both.

## 2026-10-14 · ReplayEngine.run() never terminated when the window reached the data end

- **Task:** autonomous improvement pass (cron). No board task; a defect found
  while running the test suite.
- **Need check:** repo-only. All evidence is synthetic in-memory fixtures plus the
  real `data/trading.db` read-only for the split-half confirmation. No prod file
  touched (the replay engine is a research/report path; it places no orders).
- **Found:** the full suite HANGS. `tests/loop/test_hunt5_gate.py` (added
  2026-10-13 but left **untracked**, so it never ran in CI) stalls on its first
  test forever. The hang is in `scripts/replay_engine.py`: `run()` exits only on
  `now > self.end_ms` (strict), and `advance_all()` returned `True`
  unconditionally — so when the requested window reaches or overshoots the last
  bar's close, the clock stalls (it is the MIN cursor) and the loop spins forever.
  hunt5's real sweep is masked (window ends 2026-10-01, before the data end), so
  the first caller to hit it was the new test fixture.
- **Done:** `advance_all()` now returns whether the clock actually MOVED
  (`moved`), and returns `False` at exhaustion; `run()`'s existing
  `if not self.advance_all(): break` terminates. Design + lesson:
  `docs/proposals/replay-run-nontermination-2026-10-14.md`.
- **Evidence:** RED first — `pytest tests/loop/test_replay_termination.py` hangs
  at window offsets +1/+2/+10 bars past the last close (killed). GREEN after the
  fix: 6 passed in 0.07s. Mutation check: restoring `return True` re-hangs.
  `tests/loop/` → **351 passed, 3 skipped in 39.8s** (the 4 hunt5 gate tests now
  pass). Full suite `pytest tests/` → **596 passed, 3 skipped**. Production
  unchanged: split-half PASS candidate `volbreak 20 x2.0` still passes both
  halves (IS +11.02/1.14, OOS +15.77/1.19).
- **Learned:** a loop whose only exit is a strict inequality has a boundary it
  never exits through — assert the PROGRESS condition, not just the clock
  condition, and make a dependency's boolean mean exactly what the caller trusts
  it for. Second lesson: an untracked test is not a test; `test_hunt5_gate.py`
  was written, run once by hand, and never committed, so the hang it exposes was
  invisible to CI for a day. Commit new tests with the change that motivates them.

## 2026-10-13 · the per-edge freshness guard could never clear (permanent alarm)

- **Task:** autonomous improvement pass (cron). No board task; a defect found
  while reading the improvement layer.
- **Need check:** no external facts — all evidence is the real book
  `data/unified-paper.json` and the live `improvement_loop.py`. No prod files
  touched (the loop is paper/report-only).
- **Found:** `_edge_freshness()` raised its "per-edge stats are NOT current …
  STALE" note on every run. On 2026-10-13 10:40 the report carried BOTH
  `"stale": false` (last tag 0.8d old — healthy) AND the STALE note. The
  `tagged_pct < 50` gate read the WHOLE book (499/2923 = 17.1%), but 2751/2923
  trades are LEGACY — written before the 2026-10-10 fix that made the active
  openers stamp `edge` — and can never acquire a tag. So the rate is permanently
  below 50% and the alarm can never clear. A warning that is always on is
  indistinguishable from no warning, and it MASKS the genuine freeze the guard
  exists to catch.
- **Done:** judge staleness on a RECENT tagging rate, measured from the newest
  untagged trade onward (inclusive), with a `MIN_RECENT=30` fallback to the
  whole-book rate so it can only ever be stricter. Whole-book `tagged_pct` stays
  as a reported total. Design:
  `docs/proposals/edge-freshness-permanent-alarm-2026-10-13.md`. Two alternatives
  (earliest-tag boundary, fixed time window) were tried on the real book and
  rejected — recorded in the design.
- **Evidence:** `venv/bin/python -m pytest tests/test_improvement_loop_freshness.py`
  → `7 passed`. Mutation check: reverting the judging input to `tagged_pct`
  makes `test_healthily_tagged_book_is_not_flagged_stale` FAIL. Full suite
  `586 passed, 3 skipped`. E2E on the real book: `note` is now `""`,
  `recent_tagged_pct=99.3`, and the priority-1 `stale_edge_data` suggestion no
  longer fires (only the genuine `edge_decay` remains).
- **Learned:** a ratio whose denominator includes immutable history is a ratio
  that can never recover. When a guard's job is "is this STILL being fed?", its
  input must be scoped to the population the current writers could have produced
  — not to the whole corpus. Same family as the window-coverage guard
  (2026-10-10): assert the input is present and CURRENT, not merely non-empty.

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
