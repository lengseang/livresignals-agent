# Loop journal

Newest first. Each entry: task, need check, what was done, evidence (commands and
results), and what was learned or blocked.

## 2026-11-07 (overnight shift) · the watcher lived inside the thing it watched, and its alarm exited 0

- **Task:** T0.14 (new). The board's first pickable `todo` chain was empty —
  T0.10 waits on E10, T0.12 on E11, T2.2b/T2.4 need data work whose upstream is
  unmoved — so this pass followed the loudest **live** fact on the host instead:
  a cron job that has been failing 4 times in 24 h with the same text.
- **Need check:** repo reads, `sqlite3` on `cron/executions.db` (read-only URI),
  `/opt/trading-agent-v2` read-only (heartbeat, preflight, logs, source),
  `data/trading.db` (`mode=ro`). **No writes** outside the branch, no restart, no
  pm2, no config edit, no wrapper edit, no order path touched, `trend_follower`
  and every real-money script untouched.
- **Arrival state:** branch `loop/T0.12-quiet-cron-exit` @ `3509485`, tree clean
  except four data files the refresh jobs rewrite. Cut
  `loop/v2-lane-dead-since-11-05` from it.
- **🔴 Found.** `d071de6fa4a4` failed 2026-11-05T09:00, 21:00, 11-06T09:00,
  11-06T21:00 with `paper: heartbeat is N min old — the lane is not cycling`.
  Chased it: the V2 paper lanes have written **no heartbeat since
  2026-11-05T09:00:03Z** (cycles 1728, steps 8639). Preflight now says
  `FAIL jev_key_authenticates HTTP 403`; `v2-paper.out.log` holds **427
  consecutive `PREFLIGHT FAILED`**; pm2 has been restart-looping both runners.
  All other preflight checks pass, including `paper_only: PASS` — no order path.
- **The chain, each link measured:**
  1. `run-v2.sh`'s preflight notice needs `notify_state.json`; neither lane dir
     has one, so the send failed and `|| true` dropped it. Nothing reads
     `preflight.json`.
  2. `watchdog.py`'s `runner_silent` is *correct* (heartbeat mtime, 300 s) and
     runs inside the runner, so it cannot report the runner's death. Its state
     file still says `alerted: ["runner_silent"]` — a memory, not a watcher.
  3. `scripts/v2_gate_watchdog.py` sees it and exits **1** (real run, 2405 min).
  4. `v2_gate_roll.sh` ends *both* rc branches in `exit 0` →
     `eb407360fa04` = **4 completed, 0 failed** in `executions.db`. The job that
     keeps its rc delivered all 4 times.
  5. `watchdog.digest()` prints `jev 1575/1725` = **91.3% turnout** read from a
     40-hour-old file. A ratio of two frozen counters is a frozen ratio.
- **Shipped (read-only):** `research/_v2_lane_silence_probe.py` (reproduces all
  five links from the live files), `tests/loop/test_v2_lane_silence.py`
  (**6 passed**; mutations: staleness→False ⇒ 1 failed, turnout→0.0 ⇒ 1 failed,
  harness restored by `diff -q`), `docs/proposals/v2-lane-silent-since-2026-11-05.md`,
  TASKS T0.14, **E13**.
- **Also measured, no change made (two near-misses recorded so they are not
  re-derived as findings):**
  - A SOL 5m "UP and near 24h high" entry showed +0.87%/24h gross (t=+9.5) on
    2,174 signals — then the same rule on BTC and ETH gave +0.79% and +0.90%
    with t=+11.3/+9.6. Identical numbers on three symbols is the **market**, not
    the rule. Buy-and-hold over the same 89 days is −26.4/−41.1/−57.7%, so the
    24h drift is a down-market artefact, not an edge.
  - I suspected the candle store's `1h` bars were a 1m series resampled (odd
    start 2024-12-18T12:00Z, 15,597 rows × 3 symbols). Tested it against the 5m
    series: **100% of 1h closes equal the hour's LAST 5m close.** Not a defect.
    The "927 h gaps" is one contiguous block (2025-10-06 → 2025-11-13), an
    explicit archive gap, not scattered holes. `research/_candle_1h_bar_basis.py`.
- **Reference bar measured, for the next research task:** every-bar long,
  BTC/ETH/SOL, 652 days: 7d hold −0.08/−0.00/−0.24% gross, 30d −0.06/+0.83/−0.68%,
  t between −0.8 and +0.9. **The zero-skill bar is ~0% net of fees.** Any entry
  rule that cannot clear 0.16% round trip on that bar is not an edge.
  `research/_zero_skill_reference.py`.
- **Not shipped, deliberately:** no restart, no `config/v2.env`, no pm2, no
  `V2_SYMBOLS`, no wrapper edit, no scheduler change. The credential is the
  owner's; the wrapper edit is owner-facing (E13, same family as E11).
- **Learned:** **a watcher that runs inside the thing it watches cannot report
  its death, and a wrapper that ends in `exit 0` cannot report its alarm.** The
  V2 lane had *both* — the in-process watchdog was right about the rule and
  wrong about the process, and the independent one was right about everything
  and had its exit code thrown away. The tell is cheap to check and I will run
  it on any monitor from now on: *"if this process dies right now, which line of
  code notices, and in which process does that line run?"* Second-order: the
  digest printed `jev 1575/1725` — a *ratio* of two counters written by the same
  dead process. Ratios are the most convincing form of stale data, because they
  look like they have been normalised for exactly the thing (time) that broke.

## 2026-11-06 (overnight shift) · the lab runs 96×/day and has tried 8 configurations in 6 weeks

- **Task:** T1.6 (new; not on the board when the shift started — the untracked
  `src/trading/ops/lab_repetition.py` + its 204-line test file were the arrival).
  Read-only measurement of whether `strategy_lab.py`'s 54-point grid produces
  observations or one observation repeated.
- **Need check:** repo + live lab logs (`data/strategy-lab/`) + the candle DB,
  all read-only. No order path, no parameter, no crontab, no book. **No wrapper
  edited**; the schedule decision is E12 (owner).
- **Arrival defects, both fixed:**
  1. `tests/loop/test_quiet_cron_exit.py` on `HEAD` (`6739bde`) used
     `KNOWN_UNFIXED` **without defining it** → the T0.12 ratchet guard was not
     ratcheting, it was crashing with `NameError`. Reproduced by running the
     committed file: `1 failed, 10 passed … NameError: name 'KNOWN_UNFIXED' is not
     defined`. An uncommitted definition was sitting in the tree (an
     `auto-backup` commit from a shared working tree swept up a half-finished
     edit). Kept it, added the regression test the crash showed was missing, and
     mutation-checked it: `KNOWN_UNFIXED = frozenset()` → 2 failed. 12 passed.
  2. The T1.6 module header **carried four numbers the module does not produce**:
     165,059 lines (actual 166,119), 2,121 runs since 10-16 (actual 2,141),
     "71.6% fragile" when it is **100.0% (1,538 of 1,538)**, and — the important
     one — *"last NEW configuration to clear the gate 2026-10-16"*, which was the
     **novelty date**, not the grid date. A new grid point first cleared the gate
     on **2026-10-22T09:15**. Corrected the header; added 2 tests that pin the
     derivation (mutation: `fragile` forced to 0 → the new header test goes red,
     the old 12 stay green, so it covers a real gap). 14 passed.
  3. Two smaller ones: header said "15m runs" but `strategy_lab.py:69` is
     `TIMEFRAME="1h"` and `rejected.jsonl` records carry **no** timeframe field;
     and `+~1.5 MB/day` vs measured **0.82 MB/day**.
- **⚠️ Checked before calling it a bug:** the report prints
  `last bar … (2.0 h behind the last run)`. That looks like a stalled feed, but
  the lab scores **1h** bars and cron is at :00/:15/:30/:45, so a closed 1h bar is
  legitimately 0–1h old and the run straddles the boundary. Not a defect — but it
  is only readable because the timeframe is now stated.
- **The finding, measured 2026-11-06T18:00 on 3,274 runs / 166,119 rejects:**

```text
runs                 3,274        cadence 96.2/day
grid points past the split gate   8 of 54   — ALL SOL/USDT
BTC + ETH            66.7% of the grid, ZERO points ever advanced
runs with a survivor 1,538
  … every survivor died at boundary gate  1,538  (100.0%)
last NEW grid point  2026-10-22T09:15
rejected.jsonl       27.9 MB   +0.82 MB/day
```

  **The "13.1% novel" figure must never be quoted without the 8-of-54.** A novel
  *reason string* is a moved number, not a new configuration. This is the same
  shape as every other trap on this stack: a counter that looks like learning
  (429 novel verdict sets) sitting on top of a search that has not advanced since
  October.
- **Shipped:** `src/trading/ops/lab_repetition.py` (read-only report),
  `tests/loop/test_lab_repetition.py` (14 tests, 2/2 mutations caught), TASKS
  T1.6. **Not shipped:** any change to the lab's schedule or grid — E12.
- **Suite:** `1 failed, 900 passed, 5 skipped, 1 xfailed` (148s, this turn). The
  failure is the standing daily-cache pin
  `test_research_stop_basis.py::test_the_traced_book_is_the_book_the_loop_prints`
  — known-red, unchanged, not a regression from this shift.
- **Learned:** **a module that prints numbers must not restate them in prose.**
  The header's four wrong values were all copied from an earlier draft and none
  was re-derived when the tool got better; the tool printed 100.0% while the
  header said 71.6%, and nobody read both at once. Any statistic in a docstring
  is a claim that decays on its own clock.

## 2026-11-06 · a quiet job that forces `exit 0` cannot report its own death

- **Task:** T0.12 (first pickable `todo`; T0.10's reader PR waits on E10).
  Audit every quiet cron job for the 2026-10-19 shape.
- **Need check:** repo-only plus read-only reads of `~/hermes-home/cron/`
  (`jobs.json`, `executions.db`) and the wrapper scripts. No exchange read, no
  order path, no parameter/disable list/book/live-state touched. **No wrapper was
  edited** — they are the owner's delivery surface (E11).
- **Arrival state:** `386 passed, 3 skipped, 1 failed` in `tests/loop` — the one
  failure is this task's own guard, deliberately red on the two un-fixed wrappers.
  The older daily-cache level pin was not in this selection.
- **First, measured the scheduler instead of assuming it.** `hermes cron runs` and
  `cron/executions.db` show a non-zero exit is recorded `failed` **with its full
  stdout** and delivered: job `d071de6fa4a4` failed 2026-11-05T09:00 and 21:00 and
  both `V2 GATE FAILURE` texts are in the attempt records. So the scheduler is
  not the problem — a wrapper that forces `exit 0` is, because it converts a crash
  into a `completed` run that delivers nothing.
- **🔴 Found (1).** `run_scan_ping.sh` (48×/day) and `run_trading_digest.sh`
  (1×/day) are the pre-2026-10-19 template verbatim: capture `$OUT`, echo it,
  `exit 0`, rc never read. Reproduced on a throwaway crash script: the wrapper
  prints `[SCAN] Traceback …` and **exits 0**. The failure is not hidden, it is
  *disguised* — a traceback delivered in the normal scan format, marked successful.
  `scan_ping.py` is the STALL ALERT monitor, so its own death is the one failure
  it exists to report. No failure in 88 attempts yet: latent, not active.
- **🔴 Found (2).** `run_strategy_book.sh` checks `strategy_book.py`'s rc but not
  `strategy_compact.py`'s (a `timeout 600` step), then ends `exit 0`.
- **Cleared, not assumed:** `crypto_trend_demo.sh` and `v2_gate_roll.sh` *look*
  like the defect but guard `exit 0` inside `if [ "$RC" = ... ]` blocks — a
  conditional exit is a decision, not a swallow. `run_hunt.sh` is deliberately
  silent on "nothing found" (documented) and is excluded by name. My analyser's
  first version flagged all three; that was a false-positive bug in the tool, not
  a finding, and the fix is in the diff history of this branch.
- **Shipped:** `src/trading/ops/cron_exit_audit.py` (paren-aware capture scanner,
  block-aware `exit 0` detector, `bash -n`), `tests/loop/test_quiet_cron_exit.py`
  (11 tests), `designs/T0.12.md`, E11, TASKS updated. The live-directory test is
  the regression guard: it names any wrapper that cannot report a failure, so a
  re-templated `exit 0` fails CI without anyone remembering to add it to a list.
- **Mutations run:** ignore the block-depth guard → 1 failed; treat `$(date …)` as
  a work capture → 1 failed; never detect a capture → 1 failed. Harness restored
  by `diff -q`, identical.
- **Not shipped, deliberately:** the three wrapper edits (E11: the rc-to-message
  wording is owner-facing); any change to `hunt`'s silence; any job schedule.
- **Learned:** `executions.db` is the only place a cron failure survives — the
  Telegram message scrolls away, but the attempt record keeps the stdout. And the
  dangerous version of this bug is not the silent one: `exit 0` plus an echoed
  trace delivers a *noisy* success, which reads like the job working. Ask "what
  would a crash look like here?" — if the answer is "the same as a quiet normal
  run", the rc is the only remaining channel, and it must be kept.

## 2026-11-05 (overnight shift) · the row labelled "REAL $" reads the PAPER log, and the real record is one trade

- **Task:** overnight autonomous improvement pass, following the daytime shift (below).
  That pass went to the suggestion channel; this one went to the one row in the
  owner's daily report that names a money basis — `expectancy_vs_fee.py`'s
  `trend_follower (REAL $)`.
- **Need check:** repo-only, read-only. No exchange read, no order path, no parameter,
  disable list, book or live state file touched; **neither reader was edited**
  (`expectancy_vs_fee.py`, `fee_audit.py`) — owner-facing numbers and `fee_audit.py`
  is the 09:50 cron job. Measurements read `data/trend-state.json`,
  `data/unified-paper.json`, `data/signals-state.json` and the BNB price.
- **Arrival state:** `1 failed, 876 passed, 5 skipped, 1 xfailed` — the known
  daily-cache level pin (`test_research_stop_basis.py::test_the_traced_book_is_the_book_the_loop_prints`,
  now `38.94 != 34.26`; same class as the four prior instances — a level pinned on a
  cache that refreshes; test-design debt, left as-is and named again).
- **🔴 Found.** `expectancy_vs_fee.py` prints `trend_follower (REAL $)  n=206
  +1.451%/trade t=+1.98` — read from `trend-state.json` → **`log`**, a 53-symbol PAPER
  scan log. Real money is `trend-state.json` → **`real.closed`**, and T0.10's design
  (`designs/T0.5.md` §4) called for this fix three weeks ago.
- **What the real record says (n=18):** mean exit-vs-entry **+6.19%**, median
  **−1.03%**, t=+0.97; `pnl_usd` **null on all 18**, `fee_status UNKNOWN` on all 18.
  **Remove one record (NEAR, +109.8%, `entry_status: BOOKED`) and 17 read +0.09%,
  t=+0.05** — the headline is one pre-existing position, not the book's edge. Win rate
  22.2% matches the log's 21.8%, so the sign agreement is genuine; the magnitude is not.
  `real.open` empty; last real close 2026-10-29 — the book has been idle a week and the
  report prints no such thing.
- **Fee basis is also wrong in size, not just in label.** `fee_audit.py` multiplies the
  log by a hardcoded `stake = 5.0`. The real records imply ~$5.49 notional and paid
  **0.00010317 BNB ≈ $0.071** across 18 closes on ~$96 notional — about 0.07% RT, not
  the 0.1594% the report charges. Direction of the conclusion unchanged; the net number
  is not a net number until `fee_usd` is resolved. `check-books.py` §1 unit-checks four
  books and **not** `trend-state.json` — paper gets a unit check, real money does not.
- **Shipped:** `docs/proposals/real-book-reported-on-paper-log-2026-11-05.md` (design +
  pre-mortem + 5-test plan for the T0.10 reader PR); SYSTEM_MAP #25; TASKS.md gains
  a `todo` under T0.10 with the measured evidence, and T0.13 for the missing unit check.
  No code change, therefore no mutation run — there is nothing to mutate.
- **Also measured, no change made:** the paper book on the production collapser is now
  **3619 raw → 2832 collapsed, 787 removed** — 111 more phantom rows than the
  daytime shift's 3573→2798, because the scanner wrote 106 rows since. The
  `trail (sl>=entry)` vs `risk-level` split the skill carries holds up on the larger
  book and is **stable in both halves** (trail H1 +39.74 / H2 +31.68; risk H1 −30.15 /
  H2 −34.25), and on **14 of 14 symbols** — which is the property the skill said was
  still unverified ("harness still unverified"). It is a *book-keeping* split (trails are
  never the losing side by construction once `sl >= entry`), so it is NOT evidence a
  trailing stop earns anything; quote it as a labelling fact, never as an edge.
- **Not shipped, deliberately:** any reader edit (escalated as T0.10's PR); any change to
  `trend_follower.py`'s `real.closed` write path (real-money script, T8.1 design
  unapproved); any change to the paper book, parameters, disable list or live state.
- **Learned:** **a label that names a book is not the book it reads.** `log` and
  `real.closed` are one keystroke apart in the same file and describe different
  universes; the row's label came from the design intent and the key from habit, and
  nothing in the stack compared them. Same family as the ask-2 "a hardcoded stake is a
  fabricated size" rule, and the third instance of "one record flips the sign" (see
  `demo-0004-NEARUSDT`). Write-up: the proposal above; SYSTEM_MAP #25.

## 2026-11-05 (daytime shift) · the advice channel could not close an item, and an early return was deleting what it did close

- **Task:** autonomous improvement pass, following the overnight shift (below).
  That pass went below the config dict to a literal; this one went to the
  *suggestion channel itself* — the list the owner reads every 40 minutes.
- **Need check:** repo-only. No exchange read, no order path, no parameter,
  disable list, book or live state file touched. The research loop was not run.
  Reading only: `unified-paper.json`, `reports/improvement-report.json`,
  `data/fee-audit.json`.
- **Arrival state:** `1 failed, 496 passed, 2 skipped, 1 xfailed` — the known
  daily-cache level pin (`test_research_stop_basis.py`), named again, left as-is.
- **🔴 Found (1).** `_suggest()` ended with a hardcoded literal:
  `{"priority": 4, "type": "explore", "idea": "Explore: time-of-day filter —
  capitulation bounces may cluster around US session open (13:30-16:00 UTC)…"}`.
  `git log -S` puts it at the repo's **first commit** (`cb1af99`); it printed on
  every run since. No code path could retire it. The volume-gate idea beside it
  had stopped repeating only because someone hand-edited its literal into an
  `explore_result` — which also deleted the record that it was ever tried.
- **Tested it, because an un-retired idea is a debt either way.** Design fixed
  before any outcome was seen (`research/_session_cluster_probe.py`): production
  `collapse_multitf` (3573→2798, 775 removed), **net** pnl, costed only
  (1986 uncosted dropped — all pre-2026-10-10, all `loose: true`), keyed on UTC
  hour in four a-priori bands. Book: **n=812, net −3.484, mean −0.00429**.
  `Asia 221 −3.161 t=−2.06 | London 190 +0.039 t=+0.99 | NY 328 +0.524 t=+1.64
  | Late 73 −0.886 t=−0.88`.
- **NEGATIVE, twice over.** (a) Every band flips across the 10-22 split: Asia
  H1 t=−3.24 → H2 t=+0.15; NY H1 t=+2.40 → H2 t=−0.42. (b) NY's entire +0.524 is
  **one day** — 2026-10-18 contributes +3.21, the breadth flush already
  documented in `unified-book-one-day-dependency-2026-10-23.md`. Drop it and
  NY reads t=−0.12 and the book is −6.670. London's t *rises* to +1.96 purely
  from that removal, then flips across its own halves (−0.00485 → +0.00505) and
  its hourly cells run +0.20/−0.50/−0.09/+0.83/−0.33/−0.10. An artefact that
  depends on which day you drop is not a session effect.
- **🔴 Found (2), while wiring the fix.** The `wait_for_data` gate
  (`if not paper or paper["n"] < 20`) did `return out` — dropping **section 5
  entirely**, i.e. every exploration *and every closure record*. A thin book
  says nothing about a hypothesis tested on a full one, but the early return
  deleted that knowledge silently. Verified: with `paper={'n':3}` the old path
  emitted `['wait_for_data']` and no closures.
- **Shipped.** `scripts/improvement_loop.py`: `_explorations()` holds the ideas,
  `CLOSED_HYPOTHESES` holds the verdicts; `_emit_explorations()` renders each as
  `explore` or `explore_result` by register lookup alone, so a closed idea can
  never come back as a task even if a caller passes live-sounding text. The
  `wait_for_data` path now emits the exploration list too.
  `tests/test_improvement_explore_closure.py` (5 tests).
- **Mutations run:** register ignored (`closed = None`) → 2 failed; early return
  restored → 1 failed; evidence path pointed at a nonexistent file → 1 failed.
  Harness restored by `diff -q`, identical. Suite in this session: see below.
- **Live report after the change:** 8 suggestions, both closures visible
  (`CLOSED (tested NEGATIVE 2026-10-10)` volume-spike, `… 2026-11-05` session),
  the six real findings unchanged.
- **Not shipped, deliberately:** any session filter (the evidence is negative);
  any change to the priority-1/2/3 findings; any promotion or re-rank.
- **Learned:** a suggestion channel is a *contract*, and an item with no closed
  state is a recurring charge, not a backlog item. Deleting the line is the
  wrong fix because it also deletes the negative knowledge — the loop needs a
  **closed rendering**, not an absence. And an early `return` is a silent
  policy on every section below it: it is easy to add one at a data gate and
  never notice that it also deleted a section that was not about the data.
  Write-up: `docs/proposals/session-filter-closed-2026-11-05.md`; SYSTEM_MAP #24.

## 2026-11-05 (overnight shift) · the 5% minimum position is the knob nobody chose, and it decides the ranking

- **Task:** overnight autonomous improvement pass. Yesterday's proposal left the
  sizing menu open and said the pick must precede the look; this pass did not
  pick. It went one level below the menu — to the literal in `bt()` that is not
  in any config dict at all.
- **Need check:** repo-only. No exchange read, no order path, no parameter,
  disable list, book or live state file touched. `scripts/research_loop.py` was
  **not** run (its output is the thing being measured); measurements load
  `research/06-walkforward.py` through importlib from a temp copy. One commit:
  a test file, a proposal, a SYSTEM_MAP lesson, this entry.
- **Arrival state:** `1 failed, 868 passed, 5 skipped, 1 xfailed` — the failure
  is the known daily-cache level pin
  (`test_research_stop_basis.py::test_the_traced_book_is_the_book_the_loop_prints`),
  fourth instance, left as-is and named again.
- **🔴 Found.** `run()`'s grid varies `topn/hold/risk/sma_r`. `bt()` sizes with
  `sz = min(cash·risk/σ, cash/slots)` and then `if sz < 0.05: continue`.
  **The floor is in no row and in no output.** LIVE config, same folds, one
  literal:
  `0.05 → 53.58/6.41/30.23/47.65, med 38.94, 698 tr`;
  `0.03 → 54.44/7.48/29.95/43.32, med 36.64, 709 tr`.
  **0.03, 0.02, 0.01 and none are identical** — below 3% the knob is dead, so
  "minimum position" is one step of a step function, not a parameter.
  Per-fold floored-minus-none `−0.86 −1.07 +0.28 +4.33`: the median prefers the
  floor, three of four folds do not, and fold 4 carries the whole 2.30pp.
- **Shipped.** `tests/test_research_loop_min_position_floor.py` (3 tests):
  the floor is live (with a `MUTANT_NO_FLOOR` fixture that keeps the literal and
  neutralises the skip, so the assertion bites on behaviour); the per-fold sign
  disagrees (shape, never level — #11); and the shift must stay inside one
  fold's spread so the write-up cannot quietly become an understatement.
  Mutations run: literal moved to 0.5 → 3 errors on the count assertion; test
  repointed at the mutant instead of 0.05 → `1 failed, 2 passed`. Harness
  restored by `diff -q`, identical.
- **Measured in passing (no change made).** Paper book with the production
  collapser: 3571 raw → 2796 collapsed, 775 removed, gross `+24.669 → +16.155`,
  **phantom rows +8.514 = 34.5% of raw gross** (SL 298, TIME 292, TP 107,
  STALL 78). Collapsed mean +0.578%/trade on $5 = 2.9c vs 1.4c assumed fee —
  same order, which is the number to hold next to any quoted "+$24".
- **Not shipped, deliberately:** removing or changing the floor (picking after
  reading the numbers is the pre-declared-validation error, four prior
  instances), any re-rank, any promotion, the decay question.
- **Learned:** yesterday's lesson ("check that the knob the claim is about is the
  knob that moves the number") needed one more rung. **A knob that is not a
  parameter is still a knob.** The grid can vary every key in the dict and be
  ranked by a literal three lines below it; because it never appears in a row,
  no consistency check inside the grid can see it. Write-up:
  `docs/proposals/min-position-floor-2026-11-05.md`; SYSTEM_MAP #23.

## 2026-11-04 (overnight, second pass) · the paper-fill bound's input does not exist, and its fixture never tested the rule

- **Task:** overnight autonomous improvement pass, second pass of the night. The
  first pass (below) recorded that it did not touch the paper book; this one
  picked up the untracked `scripts/paper_exit_fill.py` that the daytime shift
  left with a red test — "why is the paper book's net basis unbounded?"
- **Need check:** repo-only. No exchange read, no order path, no parameter,
  disable list, book or live state file touched. One commit touches
  `scripts/paper_exit_fill.py` and its tests; the research loop was not run.
  Reading only: `unified-paper.json`, `trading.db` (read-only URI), the ingester
  logs and the two book summaries. Suite run **in this session**:
  `869 passed, 4 skipped, 1 xfailed` with one pre-existing level pin deselected
  (see below); `tests/test_paper_exit_fill.py` alone: `12 passed, 1 xfailed`.
- **Arrival state was red, and it was a real defect, not drift.** The 2026-11-04
  daytime shift left `scripts/paper_exit_fill.py` (untracked) with
  `test_adversarial_pnl_charges_the_worst_bar_through_the_stop` failing
  `TypeError: cannot unpack non-iterable NoneType`. Fixing that exposed two more
  layers, and the second one is the finding.
- **Found (1) the fixture asserted behaviour the module never had.** Under the
  module's own rule a bar carries a bound only when `close > open` and
  `range > body`; the fixture's bars closed BELOW their opens, so `None` was the
  rule working. The rewrite reproduces the rule deliberately instead of hoping a
  bar happens to satisfy it. Also hit: `1790812800000` is 2026-10-01 00:00Z and
  the fixture comment was right; a one-bar list with an index-2 lookup raises
  `IndexError` rather than returning `None` — check the fixture, not the code.
- **Found (2) — the input does not exist.** `avg_gain_in_force()`, the constant
  the whole bar bound is built on, returns a **negative** mean at every stop
  level of the live book: −0.52% (n=421) at −1.0%, −0.94% (n=768) at −2.0%,
  −0.92% (n=495) at −2.5%. It is the mean booked outcome of trades that *closed
  near that stop* — 526 below-entry SL closers average **−2.29%** — i.e. a
  quantity selected on the outcome it is used to estimate. The reading of the
  sign in the docstring is wrong by construction and `abs()` would hide it.
  Separately, `adversarial_pnl` walks to the end of the series, not to the
  record's close, so its window is the trade's whole future — the `summarise`
  docstring already admits this. **The bar method cannot be computed from this
  book; the `min(adv, booked)` clamp is the only thing making it safe.**
- **What IS citable from that module** (`bracket_vs_level`, no bars needed):
  1,332 SL records with a constant level, **1,269 booked beyond it, 0 inside**,
  63 exactly at it, mean gap −0.52pp (below-entry n=526: 517 beyond, −0.62pp).
  **98.3% of below-entry SL fills are one-directionally worse than their own
  stop.** The write-path rounding tolerance is 0.02% and cannot explain 0.52pp.
  This says the fill is not the level; it does not say by how much.
- **Shipped (small, safe, no estimate):** `adversarial_pnl` accepts
  `avg_gain_in_force`'s `(mean, n)` tuple as well as a float and refuses a
  non-positive gain instead of sign-flipping. Two tests pin the seam and the
  refusal; `test_summarise_is_one_sided…` was the only caller-side coverage and
  it monkeypatches the bar method out, so the type contract existed nowhere.
- **Not shipped, deliberately:** any new estimate of the adverse fill (the input
  is mis-conditioned, so a number would be invented, not measured), a change to
  `summarise()`, and the `agreement()` xfail — its stated reason still holds.
  Write-up: `docs/proposals/paper-fill-bound-never-ran-2026-11-04.md`.
- **Red left standing, named:** `test_research_stop_basis.py::test_the_traced_book
  _is_the_book_the_loop_prints` is `assert 36.11 == 34.26` — the same one-bar
  cache roll the earlier shift recorded as the third instance of pinning a level
  on a daily-refreshed cache. Not touched here; it is a test-design debt that
  needs a shape assertion, not a new constant.
- **System checks in passing (not this pass's work):** 60 series current across
  10 symbols × 6 tfs, both ingesters printing per-series verdicts (18 and 42
  series), `close_time_utc > now` = 0 rows, `LIVE_ENABLED` present and git-tracked
  (not touched). Paper book collapse: 3,568 raw → 2,793 collapsed, 775 removed,
  gross +16.198 vs raw +24.712 — phantom rows are 34% of the raw gross, so any
  quoted unified-book gross must say `collapsed`.
- **Learned:** two defects stacked and the unexercised seam is what let the first
  hide the second — but the one worth carrying is the follow-on: **a proxy for
  "edge in force" selected on the trade's exit is not a proxy, it is the
  outcome.** When the conditioning variable is the quantity being estimated the
  sign is wrong by construction, and `abs()` makes it look fine. The honest
  output of this pass is a bound that cannot be computed plus one one-sided fact
  that can.

## 2026-11-04 (overnight shift) · the risk rule is a cap, and the crown is a function of it

- **Task:** overnight autonomous improvement pass. Yesterday left the decay test
  as "needs a decision about what decayed means"; this pass did **not** take that
  decision. It went after why the instrument is flat and found a different,
  larger object — the sizer.
- **Need check:** repo-only. No exchange read, no order path, no parameter,
  disable list, book or live state file touched. One write by the loop's own
  entrypoint (`research_loop.py`, 01:02 → its two files); one Telegram notice to
  `reports` (message 5500) because the finding changes what a printed number
  means and the owner reads those numbers.
- **Arrival state was red, and it was cache drift.** `36.11 == 34.26` failed:
  the 01:00 refresh rolled the window **one bar** (all 24 coins, every date key
  +1). Windows are offsets, and LIVE's fold 1 deploys 113% of fold capital, so
  its fold 1 is a cliff function of where the window starts
  (`49.59 / 5.32 / 28.34 / 43.87`, fold 1 tops at day 271 and never returns).
  Third pin-the-level-on-a-daily-cache failure; the test is left as-is and the
  shape recorded.
- **🔴 Found.** `size = min(cash·risk/σ, cash/topn)` then `if sz < 0.05: skip`.
  Liquid σ/px ≈ 0.03–0.05, so `risk/σ` is 4–7× the `1/3` cap: **the risk term
  never binds**. Measured max position **60.5% of equity** (BTC, day 289),
  median 10.7%, and a 60% name falling 10% is −6% of equity. The 2% is the
  *floor* of what the rule can express, not its target. Also: **79 rejected
  candidate entries, all `raw < cap`** — the 5% floor, not room. Repricing at a
  3% floor: 0 skips, median **33.04** vs **36.11**.
- **And the crown rides on it.** Same entries, one sizing change each: no 5%
  floor 33.04 · no topn cap 36.95 · max 30% 36.36 · max 15% 30.59 · equal weight
  **14.68** (−21.43). A nine-config grid whose ranking moves 21pp under a rule
  it does not vary is ranking the sizer — and the LIVE-vs-BTC +54.6pp headline
  with it.
- **The band is [36.11, 36.11].** Universe-only LOO over 24 coins returns one
  number. Yesterday's `[7.47, 45.64]` was one bar away and 38pp wide: that
  spread was **one-coin sensitivity of the sizing**, i.e. §2 again, not decay.
  Pinned by `tests/test_research_loop_flat_band.py` — 2 passed, 1 skipped, and
  the skip is the defect stated out loud instead of a number a refresh breaks.
- **Probe that did not pan out (recorded so it is not repeated).** I first
  suspected a basket re-rating (BTC 45k→69k across the window start). Equal
  weight buy&hold does not co-move with the book and a zero-wedge rebooking
  costs only 4.6pp of the median. Not a start-bar artefact.
- **Not shipped:** sizing. Choosing a sizing rule after reading the numbers
  above is the pre-declared-validation error this journal has recorded four
  times. §3 of the write-up is the menu; the pick has to precede the look.
  `docs/proposals/risk-pct-is-a-cap-2026-11-04.md`.
- **Learned:** yesterday's lesson was "a consistent instrument can make a test
  vacuous". This one is the step before it: **check that the knob the claim is
  about is the knob that moves the number.** The loop varies `topn/hold/risk/sma_r`
  and attributes the ranking to them, while `risk` silently does nothing above
  the cap and the *cap* does everything. A parameter that cannot bind is not a
  parameter, it is a comment — and a grid ranked on it inherits the luck of the
  thing that did bind: where the window starts.

## 2026-11-03 (overnight shift) · the leave-one-out band was a sample, and a consistent band cannot decay

- **Task:** overnight autonomous improvement pass. The daytime shift left one
  finding explicitly **unfixed** ("Proposal only; script untouched") — the
  research loop's fragility band is computed from 12 seeded draws that collide.
  This pass shipped that repair and measured what the repaired instrument says.
  Suite run last: **0 failed** (see Evidence).
- **Need check:** repo-only. `research_loop.py` is a research cron, not an
  order path. No book, parameter, disable list or state file touched; the only
  writes are the loop's own `reports/research-findings.md` and
  `data/research-loop.json`, which it regenerates every run.
- **Shipped.** `LOO_DRAWS = None` and `coin_fragility()` now drop **every
  non-BTC coin once** instead of sampling 12 of 23. Payload gains `basis` and
  `distinct_universes`; `lead_is_coin_fragile()` gains `live_basis`; the
  `LIVE EDGE DECAYED` sentence and the findings file append the basis inline.
  Whole run costs **4.3s** — sampling a population of 23 was never warranted.
- **Confirmed the artefact.** 12 draws over 23 coins = **8 distinct universes**
  (DOGE 3x, SOL 2x, LTC 2x, five once). Published ceiling `+30.20` (LTC draw);
  exhaustive ceiling is `+45.64` (**AAVE**), which was never drawn. Floor
  `+7.47` (DOGE) was right by luck. The LIVE median 34.26 is **inside** the
  honest band — the morning's "above its own ceiling" was a statement about
  the seed, now confirmed in code rather than prose.
- **🔴 Found (the part that matters).** With `median` and band from the same
  operation, the incumbent sits **inside its own band on all 6 measured rows**
  (`34.26 in [7.47,45.64]`, `8.44 in [4.76,18.35]`, …). Leave-one-out over 23
  coins is a small perturbation, so the band is necessarily wide — meaning
  `live_fragile` can essentially **never fire**. The sampling bug was the only
  thing making the decay branch reachable; fixing it removed the branch's
  reachability rather than repairing it. Same shape as the ensemble guard that
  labelled a defect for 8 days without deciding anything. **Not fixed here**:
  replacing the test needs a decision about what "decayed" means, and choosing
  that after seeing the data is the pre-declared-validation error this journal
  has recorded four times. Recorded as the next research task instead.
- **Not a finding, stated so it is not re-derived.** 2 of 6 rows invert LOO
  (`3/14` +12.50pp on 22 of 23 drops; `2/21` +1.42pp). I looked for a rule —
  by selector (`topn`) and by rank — and none holds: the inverted rows are the
  two weakest, while the crown is 1/23 and another `topn=3` row is 17/23.
  Nothing to ship.
- **Evidence.** `pytest tests/test_research_loop_coin_fragility.py -q`:
  `23 passed, 1 skipped`. **Mutation check** by file swap (`git show HEAD:` →
  run → restore → `diff` empty): `4 failed, 19 passed` on the pre-change file,
  and the 4 failures are exactly the 4 new tests. Loop re-run with the fix
  reproduces the existing book **unchanged** (`49.07/6.66/26.47/42.05`, median
  34.26, 697 trades) and emits **no change** — silence by design. Only the
  fragility band moved; no median, fold count or trade count did.
- **Anti-slop.** Three fixture errors caught before the tests were believed,
  and recorded because each would have passed vacuously: the baseline `bt()`
  call counted as a 13th "drop"; a `prev` row that TIED the challenger so the
  decay branch was never entered; and an incumbent placed **above** its
  ceiling when the realistic decay shape is **below** its floor — the
  above-ceiling version tripped the *challenger's* refusal instead and the
  assertion read the wrong branch. Also corrected two of my own draft claims
  against the data ("negative-sum for 4 of 6 rows" → 2 of 6; "topn=4 behaves
  the same" → untested).
- **Learned:** a min/max over a sampled population is a statement about the
  SEED until the draws are de-duplicated or exhausted — and the tell is
  duplication (12 draws should not contain one outcome 3 times). But the
  deeper lesson is the second order one: **making the instrument consistent
  can make the test it feeds vacuous.** A test comparing X against a
  leave-one-out of X is not asking whether X is decaying; a wide enough band
  answers "not outside" by construction. Fixing the measurement exposed that
  the question was malformed, which the biased version had been hiding.
  Write-up: `docs/proposals/loo-band-was-a-sample-2026-11-03.md`.

## 2026-11-03 (daytime shift) · the research book's stop is a floor, and its fragility band is a sample

- **Task:** daytime autonomous improvement pass (cron). The board's `todo` items
  are research/design tasks with unmet deps (T3.0 needs T2.6, T3.1 needs T3.0);
  the morning pass (#21) left two unmeasured claims in its own text, so this
  pass measured those. Suite run first: **844 passed, 5 skipped, 0 failed**.
- **Need check:** repo-only. No exchange read, no order path, no parameter,
  disable list or state file touched. `scripts/research_loop.py` unchanged.
- **Instrument.** Re-derived `bt()` with two fields it never records (close
  reason, worst adverse excursion). It reproduces all four folds to the cent —
  `49.07 / 6.66 / 26.47 / 42.05`, median `34.26`, 341 trades — so the added
  fields are free. Pinned by
  `tests/test_research_stop_basis.py::test_the_traced_book_is_the_book_the_loop_prints`.
- **Found (1) the stop is a floor on the tail.** `hold 128 · sma50 95 · stop
  118`. All 118 stop exits book at −10.18% or worse (mean −14.28%, worst
  −32.21%) because the loop sees day-start prices and exits at the close
  through the gap. **Not one** of the 341 trades ever saw a −10% excursion;
  median worst excursion −1.35%. Win rate 37.2% overall, **57.0%** once stop
  exits are removed — the stop alone accounts for the gap, so changing its
  width moves the tail, not the win rate. Same conditional structure #20 found
  in the paper book, now with a baseline that reproduces.
- **Found (2) the LOO band is a sampling artefact in both tails.**
  `run()` builds `per_coin` from **12 seeded draws over 22 non-BTC coins**; on
  today's cache those 12 draws are **8 distinct universes**, and the band is
  min/max over them: floor `7.47` is the **DOGE draw three times** (one at the
  first shuffle position, where LOO never lands in 6 of 12 draws), ceiling
  `30.20` is the **LTC draw twice** (the luckiest name). `lead_is_coin_
  fragile()` then puts the 24-coin LIVE median (34.26) beside that interval and
  calls it outside — the "regime break, not decay" sentence. **A full
  leave-one-out over all 23 non-BTC coins gives 15.04..46.57: 34.26 is
  INSIDE.** The morning entry's "above its ceiling" is therefore a statement
  about 8 draws, not about the config. No verdict flips today (the loop is
  silent when it crowns the incumbent); the decay sentence is one refresh from
  being emitted on the wrong interval.
- **Found (3) the crown moves on noise.** `150` 34.26 vs `200` 31.83 vs `100`
  30.95: margins 2.43pp and 3.31pp against a **9.64pp** spread from dropping one
  coin. Neither sibling could pass the loop's own rule if it won; the rule is
  never applied to a crown's neighbours. Where the crown and 200 differ, the
  crown wins 3 of 5 non-identical 150d segments — "not distinguishable", not
  "200 is worse".
- **Not shipped.** The two script repairs (hold the universe fixed and report a
  quantile band; stop calling the −10% a loss cap in the findings file) change
  what the owner is shown, so they are written up, not committed:
  `docs/proposals/research-book-stop-and-universe-2026-11-03.md`. No promotion,
  no pause, no config change implied.
- **Correction (reported straight, both ways).** The 01:30 entry and the
  morning proposal both say the research book has **"no fees"**. It has
  `FEE=0.00144` charged on every exit; what it lacks is measured spread and a
  shared-account constraint. The error ran in the pessimistic direction, which
  is still an error. Fixed in the new proposal; the old texts stand as written
  and are corrected by reference.
- **Learned:** a baseline can be exact and the conclusion derived from it can
  still be a property of the *sampler*. The band quoted beside 34.26 had the
  right formula, the right fee, the right baseline — and eight universes. The
  tell is duplication: 12 draws should not contain the same three outcomes
  three times. Any min/max over a seeded sample is a statement about the seed
  until the draws are de-duplicated or exhausted.

## 2026-11-03 · the research loop was refusing the incumbent as its own challenger

- **Task:** autonomous improvement pass (cron). No board task; board items are
  owner-gated or design-only. Suite was run first, on the freshly refreshed
  cache.
- **Need check:** repo-only. No exchange read, no order path, no parameter,
  disable list or live state file touched. One live write: `research_loop.py`
  regenerated `reports/research-findings.md` and `data/research-loop.json` by
  its normal entrypoint (01:07, 01:11).
- **Found (0) the arrival state was red.** Full suite **837 passed, 1 failed** —
  `test_the_solo_challenger...` asserting the crown's `sma_r` family has one
  member *on live data*. The 01:00 refresh re-ranked the grid: crown moved from
  `sma_r=100` (solo) to `sma_r=150` (six siblings). Confirmed by re-running the
  test against `git show HEAD:research/price-cache-1000d.json`: both tests in
  that file pass on the old cache and the current cache differs only by one
  appended bar on 24 of 25 coins. So it is a frozen ranking, not a defect
  (#11). Fixed by asserting the *property* (whatever is crowned is measured)
  and skipping when the crown has siblings.
- **Found (1) the defect, while asking why the crown moved.**
  `_change_lines()` calls `lead_is_coin_fragile(best_row, live_row)`, and
  `best_row = max(rows, key=score)` where LIVE is one of the rows. Whenever the
  incumbent is still best — the healthy, usual outcome — `best_row is
  live_row`: lead 0.00pp, and all three refusal triggers fire on a row compared
  with itself. On the 01:07 grid the verdict read
  *"dropping DOGE alone cuts the challenger's median 26.79pp -> lead -26.79pp;
  only 0% of 150d segments prefer it"* about `sma_r=150` vs `sma_r=150`. The
  2026-10-24 fix stopped the NEW BEST and DECAYED branches contradicting *each
  other*; it did not stop one branch contradicting itself.
- **Found (2) the tie, which the same run surfaced.** `score()` is
  `(pos_folds, median_2dp)` and `max()` returns the first; LIVE is second in
  the grid, so an exact tie ALWAYS crowns the challenger and the refusal branch
  then refuses it for a 0.00pp "lead". The 01:05 run with the incumbent guard
  alone printed `NEW BEST config: {sma_r: 150} …` — the live config announced
  as its own challenger. A refusal or an announcement whose content is decided
  by the order of a list is not a verdict.
- **Done.** `lead_is_coin_fragile()` reports `is_incumbent` and emits no
  refusal reason for that row (the three triggers are untouched for a real
  challenger; `live_fragile` is still answered, since it is the only question
  the row can answer). `_tied_challengers()` makes a tie a stated TIE ("No
  config change is implied"). `_fragility_line()` stops writing *"best vs LIVE
  +0.00pp; holds"* into the findings file for the incumbent and states its band
  instead. Lesson recorded as SYSTEM_MAP #21; design in
  `docs/proposals/incumbent-is-not-a-challenger-2026-11-03.md`.
- **Evidence.** RED first: 4 new tests failed on HEAD (`is_incumbent` absent,
  two `KeyError: 'cfg'`). GREEN: file **19 passed, 1 skipped**; full suite
  **845 passed, 4 skipped, 0 failed**. Four mutations, each caught: remove the
  incumbent guard; exempt on `lead == 0` rather than identity (a point-tie
  challenger must still be refused); remove the tie branch; define a tie by
  fold count alone. Real run: `no change (silence by design)`.
- **Not claimed.** The research loop is a simulation: 1000 daily bars × 25
  coins from `research/price-cache-1000d.json`, regime L/S, no fees, one
  account. Its LIVE row is currently ABOVE its own leave-one-coin-out ceiling
  (34.26% vs `[+7.47, +30.20]`) — a regime-break flag on a simulation, not a
  P&L statement, and the same state the 2026-10-20 entry recorded. No config
  was promoted; the incumbent stands on its own numbers, not because a guard
  was silenced.
- **Learned:** a guard is a function of TWO populations — the one the verdict
  is computed on (#14/#15) and the one the checker is handed. Here the second
  one contained the answer. The tell was in the output all along: a refusal
  whose quoted lead is exactly `+0.00pp`, and a refusal that names the same
  numbers as the incumbent. Any self-comparison passes every test that asks
  "is it different?"; the fix is an identity check, never a tighter threshold.

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
